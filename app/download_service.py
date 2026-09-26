from __future__ import annotations

import asyncio
import logging
import os
import random
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from telethon import errors
from telethon.tl import types

from .config import Config
from .database import Database
from .file_service import (copy_to_part, enriched_for_original, make_item, make_profile_item,
                           media_kind, part_path, photo_download_spec, remove_empty_directory,
                           remove_file, valid_file)
from .metadata_service import MetadataService
from .models import Channel, DownloadError, MediaItem, MetadataError, Status, Summary, UnsupportedMetadata
from .telegram_service import TelegramService
from .utils import durable_flush, iso_utc, sha256_file, utc_now


class DownloadService:
    def __init__(self, config: Config, database: Database, telegram: TelegramService,
                 metadata: MetadataService, logger: logging.Logger) -> None:
        self.config = config
        self.database = database
        self.telegram = telegram
        self.metadata = metadata
        self.logger = logger

    async def run(self, channel: Channel, entity: types.Channel, limit: int | None,
                  from_date: datetime | None, to_date: datetime | None) -> Summary:
        summary = Summary(channel=f"@{channel.username}")
        self.logger.info("Run started: channel=%s limit=%s from=%s to=%s", channel.username, limit, from_date, to_date)
        if channel.photo_id is not None:
            print("Checking channel profile photo...")
            await self._process(make_profile_item(channel, self.config.output), entity, summary)
        print(f"Downloading posts from @{channel.username}...")
        last_group: int | None = None
        async for message in self.telegram.iter_messages(entity, None):
            if not isinstance(message, types.Message):
                continue
            if from_date is not None and message.date < from_date:
                break
            if to_date is not None and message.date > to_date:
                continue
            group_id = int(message.grouped_id) if message.grouped_id is not None else None
            if limit is not None and summary.processed_posts >= limit and (group_id is None or group_id != last_group):
                break
            if group_id is None or group_id != last_group:
                summary.processed_posts += 1
                if summary.processed_posts % 50 == 0:
                    print(f"Processed {summary.processed_posts} posts...")
            last_group = group_id
            kind = media_kind(message)
            if kind is None:
                continue
            date_utc = message.date.astimezone(timezone.utc).strftime("%Y-%m-%d")
            anchor_id = message.id
            if group_id is not None:
                anchor_id, date_utc = await self.database.group_anchor(channel.id, group_id, message.id, date_utc)
            post_url = f"{channel.url}/{message.id}"
            await self.database.upsert_message(channel.id, message.id, group_id, iso_utc(message.date), post_url)
            item = make_item(channel, message, kind, self.config.output, anchor_id, date_utc)
            await self._process(item, message, summary)
        self.logger.info("Run finished: channel=%s posts=%d downloaded=%d enriched=%d skipped=%d failed=%d",
                         channel.username, summary.processed_posts, summary.downloaded_originals,
                         summary.created_enriched_copies, summary.skipped_valid_files, summary.failed_files)
        return summary

    async def _process(self, item: MediaItem, source: types.Message | types.Channel, summary: Summary) -> None:
        candidate = item.original_path
        index = 1
        while await self.database.path_in_use(candidate, item.key):
            index += 1
            candidate = item.original_path.with_name(f"{item.original_path.stem}_media-{item.file_id}_{index}{item.original_path.suffix}")
        if candidate != item.original_path:
            item = replace(item, original_path=candidate,
                           enriched_path=enriched_for_original(candidate))
        row = await self.database.upsert_media(item)
        item = replace(
            item, original_path=Path(row["original_path"]), enriched_path=Path(row["enriched_path"]),
            download_date=datetime.fromisoformat(row["download_date_utc"].replace("Z", "+00:00")),
        )
        item = await self._recover_flat_original(item, row)
        original_ok = valid_file(item.original_path, row["actual_size"], row["original_sha256"])
        if not original_ok and item.original_path.is_file() and not row["original_sha256"]:
            size = item.original_path.stat().st_size
            if size > 0 and (item.expected_size is None or size == item.expected_size):
                digest = sha256_file(item.original_path)
                await self.database.set_state(item, original=Status.DOWNLOADED, actual_size=size,
                                              downloaded_bytes=size, original_hash=digest)
                original_ok = True
                self.logger.info("Recovered original after interrupted database update: %s", item.key)
        original_was_valid = original_ok
        if not original_ok:
            had_damage = bool(row["original_sha256"] or row["enriched_sha256"])
            if had_damage:
                await self.database.set_state(item, original=Status.CORRUPTED, enriched=Status.CORRUPTED)
                self.logger.warning("Corrupted or missing original: %s", item.key)
            try:
                await self._download_original(item, source, row, summary)
                summary.downloaded_originals += 1
                if had_damage:
                    summary.repaired_corrupted_files += 1
                refreshed = await self.database.get_media(item.channel_id, item.key)
                item = replace(item, download_date=datetime.fromisoformat(refreshed["download_date_utc"].replace("Z", "+00:00")))
                if row["original_sha256"] and refreshed["original_sha256"] != row["original_sha256"]:
                    await self.database.invalidate_enriched(item)
                    remove_file(item.enriched_path)
            except Exception as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                reason = (f"Original download failed: {exc}" if isinstance(exc, DownloadError)
                          else f"Original download failed ({type(exc).__name__}).")
                await self.database.set_state(item, original=Status.FAILED, error=reason)
                self.logger.error("%s media=%s", reason, item.key)
                summary.failed_files += 1
                return
        else:
            self.logger.info("Original verified: %s", item.key)
        row = await self.database.get_media(item.channel_id, item.key)
        has_metadata, no_metadata_reason = await self.metadata.has_embedded_metadata(item.original_path, item)
        if not has_metadata:
            remove_file(item.enriched_path)
            remove_file(part_path(item.enriched_path))
            item = await self._flatten_original(item, row)
            remove_empty_directory(item.enriched_path.parent)
            await self.database.mark_metadata_unavailable(item, no_metadata_reason)
            summary.unsupported_metadata_files += 1
            if original_was_valid:
                summary.skipped_valid_files += 1
            self.logger.warning("Embedded metadata unavailable for %s: %s", item.key, no_metadata_reason)
            return
        if valid_file(item.enriched_path, None, row["enriched_sha256"]):
            if row["enriched_status"] != Status.COMPLETE:
                await self.database.set_state(item, enriched=Status.COMPLETE)
            if original_was_valid:
                summary.skipped_valid_files += 1
            self.logger.info("Enriched copy verified: %s", item.key)
            return
        had_corrupted_copy = bool(row["enriched_sha256"])
        if had_corrupted_copy:
            self.logger.warning("Corrupted or missing enriched copy: %s", item.key)
        prior_created = summary.created_enriched_copies
        await self._enrich(item, summary)
        if had_corrupted_copy and summary.created_enriched_copies > prior_created:
            summary.repaired_corrupted_files += 1

    async def _recover_flat_original(self, item: MediaItem, row: object) -> MediaItem:
        legacy = item.original_path
        if legacy.parent.name != "original" or legacy.exists() or not row["original_sha256"]:
            return item
        parent = legacy.parent.parent
        if not parent.is_dir():
            return item
        for candidate in parent.iterdir():
            if (candidate.is_file() and not candidate.is_symlink() and candidate.suffix == legacy.suffix
                    and candidate.stem.startswith(legacy.stem)
                    and not await self.database.path_in_use(candidate, item.key)
                    and valid_file(candidate, row["actual_size"], row["original_sha256"])):
                enriched = enriched_for_original(candidate)
                await self.database.update_paths(item, candidate, enriched)
                remove_empty_directory(legacy.parent)
                return replace(item, original_path=candidate, enriched_path=enriched)
        return item

    async def _flatten_original(self, item: MediaItem, row: object) -> MediaItem:
        legacy = item.original_path
        if legacy.parent.name != "original":
            return item
        parent = legacy.parent.parent
        candidate = parent / legacy.name
        index = 1
        while (await self.database.path_in_use(candidate, item.key)
               or candidate.is_symlink()
               or part_path(candidate).exists()
               or (candidate.exists() and not valid_file(candidate, row["actual_size"], row["original_sha256"]))):
            index += 1
            candidate = parent / f"{legacy.stem}_media-{item.file_id}_{index}{legacy.suffix}"
        if legacy.exists():
            if candidate.exists():
                remove_file(legacy)
            else:
                os.replace(legacy, candidate)
        enriched = enriched_for_original(candidate)
        await self.database.update_paths(item, candidate, enriched)
        remove_empty_directory(legacy.parent)
        return replace(item, original_path=candidate, enriched_path=enriched)

    async def _download_original(self, item: MediaItem, source: types.Message | types.Channel,
                                 row: object, summary: Summary) -> None:
        item.original_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = part_path(item.original_path)
        offset = 0
        resumed = False
        if temporary.exists():
            size = temporary.stat().st_size
            recorded = int(row["downloaded_bytes"])
            if (item.kind != "profile" and size == recorded and size > 0
                    and (size % 4096 == 0 or size == item.expected_size)
                    and (item.expected_size is None or size <= item.expected_size)
                    and row["telegram_file_id"] == item.file_id):
                offset = size
                resumed = True
                self.logger.info("Resuming media %s at byte %d", item.key, offset)
            else:
                remove_file(temporary)
                self.logger.warning("Unsafe partial file removed for %s", item.key)
        await self.database.set_state(item, original=Status.DOWNLOADING, downloaded_bytes=offset,
                                      attempt_at=iso_utc(utc_now()))
        if item.kind == "profile":
            assert isinstance(source, types.Channel)
            with temporary.open("wb") as destination:
                if not await self.telegram.download_profile(source, destination):
                    raise DownloadError("Channel profile photo is unavailable.")
                durable_flush(destination)
        else:
            assert isinstance(source, types.Message)
            media = source.media
            if source.photo:
                photo_location, _ = photo_download_spec(source.photo)
                if photo_location is not None:
                    media = photo_location
            for attempt in range(1, self.config.max_attempts + 1):
                try:
                    with temporary.open("ab" if offset else "wb") as destination:
                        async for chunk in self.telegram.iter_chunks(media, offset, item.expected_size):
                            destination.write(chunk)
                            durable_flush(destination)
                            offset += len(chunk)
                            await self.database.set_state(item, downloaded_bytes=offset)
                    break
                except errors.FloodWaitError as exc:
                    self.logger.warning("FloodWait downloading %s: %d seconds", item.key, exc.seconds)
                    await asyncio.sleep(max(1, exc.seconds))
                except (OSError, TimeoutError, ConnectionError, errors.ServerError, errors.TimedOutError) as exc:
                    self.logger.warning("Download retry %s: %s attempt=%d", item.key, type(exc).__name__, attempt)
                    if attempt == self.config.max_attempts:
                        raise DownloadError("Media download failed after retries.") from None
                    await asyncio.sleep(min(30, 2 ** (attempt - 1)) + random.uniform(0, 0.5))
                offset = temporary.stat().st_size if temporary.exists() else 0
            else:
                raise DownloadError("Media download failed after retries.")
        size = temporary.stat().st_size
        if size == 0 or (item.expected_size is not None and size != item.expected_size):
            await self.database.set_state(item, original=Status.CORRUPTED, downloaded_bytes=size,
                                          error="Downloaded size differs from expected size.")
            raise DownloadError(f"Downloaded size mismatch: expected {item.expected_size}, got {size}.")
        digest = sha256_file(temporary)
        os.replace(temporary, item.original_path)
        await self.database.set_state(item, original=Status.DOWNLOADED, downloaded_bytes=size,
                                      actual_size=size, original_hash=digest)
        await self.database.set_download_date(item, iso_utc(utc_now()))
        if resumed:
            summary.resumed_downloads += 1
        await self.database.clear_error(item)
        self.logger.info("Original downloaded and verified: %s bytes=%d sha256=%s", item.key, size, digest)

    async def _enrich(self, item: MediaItem, summary: Summary) -> None:
        temporary: Path | None = None
        try:
            await self.database.set_state(item, enriched=Status.ENRICHING)
            temporary = copy_to_part(item.original_path, item.enriched_path)
            await self.metadata.enrich(temporary, item)
            digest = sha256_file(temporary)
            row = await self.database.get_media(item.channel_id, item.key)
            if not valid_file(item.original_path, row["actual_size"], row["original_sha256"]):
                raise MetadataError("Original changed while creating enriched copy.")
            os.replace(temporary, item.enriched_path)
            await self.database.set_state(item, enriched=Status.COMPLETE, enriched_hash=digest)
            await self.database.clear_error(item)
            summary.created_enriched_copies += 1
            self.logger.info("Enriched copy verified: %s sha256=%s", item.key, digest)
        except UnsupportedMetadata as exc:
            if temporary is not None:
                remove_file(temporary)
            remove_empty_directory(item.enriched_path.parent)
            await self.database.set_state(item, enriched=Status.UNSUPPORTED_METADATA, error=str(exc))
            summary.unsupported_metadata_files += 1
            self.logger.warning("Metadata unsupported for %s: %s", item.key, exc)
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            if temporary is not None:
                remove_file(temporary)
            remove_empty_directory(item.enriched_path.parent)
            reason = f"Enrichment failed ({type(exc).__name__})."
            await self.database.set_state(item, enriched=Status.FAILED, error=reason)
            summary.failed_files += 1
            self.logger.error("%s media=%s", reason, item.key)
