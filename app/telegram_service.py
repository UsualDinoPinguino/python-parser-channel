from __future__ import annotations

import asyncio
import getpass
import logging
import os
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from telethon import TelegramClient, errors
from telethon.sessions import SQLiteSession
from telethon.tl import types

from .config import Config
from .logging_config import SecretFilter
from .models import AuthenticationError, Channel, ChannelError, DownloadError
from .utils import safe_name


T = TypeVar("T")


class TelegramService:
    def __init__(self, config: Config, logger: logging.Logger, redactor: SecretFilter) -> None:
        session_path = Path.cwd() / ".telegram.session"
        if session_path.is_symlink():
            raise AuthenticationError("Telegram session path must not be a symbolic link.")
        descriptor = os.open(session_path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(descriptor)
        session_path.chmod(0o600)
        self.client = TelegramClient(
            SQLiteSession(str(session_path)), config.api_id, config.api_hash,
            flood_sleep_threshold=0, request_retries=0, connection_retries=2,
        )
        self.logger = logger
        self.redactor = redactor
        self.max_attempts = config.max_attempts
        self.semaphore = asyncio.Semaphore(config.concurrency)

    async def __aenter__(self) -> "TelegramService":
        try:
            await self.retry(self.client.connect, "connect")
            if not await self.client.is_user_authorized():
                await self.authorize()
            return self
        except Exception:
            await self.client.disconnect()
            raise

    async def __aexit__(self, *_: object) -> None:
        await self.client.disconnect()

    async def authorize(self) -> None:
        try:
            phone = input("Phone number: ").strip()
            self.redactor.add_secret(phone)
            if not phone:
                raise AuthenticationError("A phone number is required.")
            sent = await self.retry(lambda: self.client.send_code_request(phone), "send code")
            code = getpass.getpass("Telegram code: ").strip()
            self.redactor.add_secret(code)
            try:
                await self.retry(lambda: self.client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash), "sign in")
            except errors.SessionPasswordNeededError:
                password = getpass.getpass("2FA password: ")
                self.redactor.add_secret(password)
                await self.retry(lambda: self.client.sign_in(password=password), "2FA sign in")
            if not await self.client.is_user_authorized():
                raise AuthenticationError("Telegram authorization did not complete.")
        except (EOFError, KeyboardInterrupt):
            raise AuthenticationError("Telegram authorization was cancelled.") from None
        except errors.RPCError:
            raise AuthenticationError("Telegram rejected the authorization request.") from None

    async def retry(self, operation: Callable[[], Awaitable[T]], label: str) -> T:
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await operation()
            except errors.FloodWaitError as exc:
                self.logger.warning("FloodWait during %s: %d seconds", label, exc.seconds)
                await asyncio.sleep(max(1, exc.seconds))
            except (OSError, TimeoutError, ConnectionError, errors.ServerError, errors.TimedOutError) as exc:
                self.logger.warning("Transient %s failure: %s, attempt %d", label, type(exc).__name__, attempt)
                if attempt == self.max_attempts:
                    raise DownloadError(f"Telegram operation failed: {label}.") from None
                await asyncio.sleep(min(30, 2 ** (attempt - 1)) + random.uniform(0, 0.5))
        raise DownloadError(f"Telegram operation failed after retries: {label}.")

    async def resolve_channel(self, username: str, known: object | None = None) -> tuple[Channel, types.Channel]:
        entity: object
        try:
            entity = await self.retry(lambda: self.client.get_entity(username), "resolve channel")
        except (ValueError, errors.RPCError):
            if known is None:
                raise ChannelError("Public channel is unavailable or does not exist.") from None
            entity = await self._resolve_known_channel(known)
        if known is not None and isinstance(entity, types.Channel) and entity.id != int(known["id"]):
            entity = await self._resolve_known_channel(known)
        if not isinstance(entity, types.Channel) or not entity.broadcast or entity.access_hash is None:
            raise ChannelError("The target must be an accessible public broadcast channel.")
        public_username = self._public_username(entity, username)
        if public_username is None:
            raise ChannelError("The target must be an accessible public broadcast channel.")
        photo_id = getattr(entity.photo, "photo_id", None)
        channel = Channel(entity.id, public_username, safe_name(public_username), photo_id, entity.access_hash)
        return channel, entity

    @staticmethod
    def _public_username(entity: types.Channel, requested: str) -> str | None:
        names = [entity.username] if entity.username else []
        names.extend(entry.username for entry in entity.usernames or [] if entry.active and entry.username)
        return next((name for name in names if name.casefold() == requested.casefold()), names[0] if names else None)

    async def _resolve_known_channel(self, known: object) -> types.Channel:
        try:
            peer = types.InputPeerChannel(int(known["id"]), int(known["access_hash"]))
            entity = await self.retry(lambda: self.client.get_entity(peer), "resolve renamed channel")
        except (ValueError, errors.RPCError):
            raise ChannelError("Channel is unavailable or access was denied.") from None
        if not isinstance(entity, types.Channel):
            raise ChannelError("Stored channel identity is invalid.")
        return entity

    async def iter_messages(self, entity: types.Channel, limit: int | None) -> AsyncIterator[types.Message]:
        remaining = limit
        offset_id = 0
        failures = 0
        while remaining is None or remaining > 0:
            batch_limit = min(100, remaining) if remaining is not None else 100
            yielded = 0
            try:
                async for message in self.client.iter_messages(entity, limit=batch_limit, offset_id=offset_id):
                    offset_id = message.id
                    yielded += 1
                    failures = 0
                    if remaining is not None:
                        remaining -= 1
                    yield message
                if yielded < batch_limit:
                    return
            except errors.FloodWaitError as exc:
                self.logger.warning("FloodWait during history: %d seconds", exc.seconds)
                await asyncio.sleep(max(1, exc.seconds))
            except (OSError, TimeoutError, ConnectionError, errors.ServerError, errors.TimedOutError) as exc:
                failures += 1
                self.logger.warning("History retry after %s: %d", type(exc).__name__, failures)
                if failures >= self.max_attempts:
                    raise DownloadError("Channel history could not be read.") from None
                await asyncio.sleep(min(30, 2 ** (failures - 1)) + random.uniform(0, 0.5))
            except errors.RPCError:
                raise ChannelError("Channel history is unavailable or access was denied.") from None

    async def iter_chunks(self, media: object, offset: int, expected_size: int | None) -> AsyncIterator[bytes]:
        async with self.semaphore:
            async for chunk in self.client.iter_download(
                media, offset=offset, request_size=524288, chunk_size=524288, file_size=expected_size,
            ):
                yield bytes(chunk)

    async def download_profile(self, entity: types.Channel, destination: object) -> bool:
        async with self.semaphore:
            result = await self.retry(lambda: self.client.download_profile_photo(entity, file=destination), "profile photo")
            return result is not None
