from __future__ import annotations

from pathlib import Path

import aiosqlite

from .file_service import enriched_for_original
from .models import Channel, MediaItem, Status
from .utils import iso_utc


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    folder TEXT NOT NULL UNIQUE,
    photo_id INTEGER,
    access_hash INTEGER NOT NULL,
    last_seen_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media_groups (
    channel_id INTEGER NOT NULL REFERENCES channels(id),
    group_id INTEGER NOT NULL,
    anchor_message_id INTEGER NOT NULL,
    date_utc TEXT NOT NULL,
    PRIMARY KEY(channel_id, group_id)
);
CREATE TABLE IF NOT EXISTS messages (
    channel_id INTEGER NOT NULL REFERENCES channels(id),
    message_id INTEGER NOT NULL,
    group_id INTEGER,
    publication_date_utc TEXT NOT NULL,
    post_url TEXT NOT NULL,
    PRIMARY KEY(channel_id, message_id)
);
CREATE TABLE IF NOT EXISTS media (
    channel_id INTEGER NOT NULL REFERENCES channels(id),
    media_key TEXT NOT NULL,
    message_id INTEGER,
    group_id INTEGER,
    telegram_file_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_name TEXT,
    mime_type TEXT,
    expected_size INTEGER,
    actual_size INTEGER,
    original_path TEXT NOT NULL,
    enriched_path TEXT NOT NULL,
    original_sha256 TEXT,
    enriched_sha256 TEXT,
    publication_date_utc TEXT,
    download_date_utc TEXT NOT NULL,
    post_url TEXT NOT NULL,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    original_status TEXT NOT NULL DEFAULT 'pending',
    enriched_status TEXT NOT NULL DEFAULT 'pending',
    last_error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_utc TEXT,
    PRIMARY KEY(channel_id, media_key),
    FOREIGN KEY(channel_id, message_id) REFERENCES messages(channel_id, message_id)
);
CREATE INDEX IF NOT EXISTS media_group_index ON media(channel_id, group_id);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: aiosqlite.Connection | None = None

    async def __aenter__(self) -> "Database":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.executescript(SCHEMA)
        await self.connection.commit()
        return self

    async def __aexit__(self, *_: object) -> None:
        if self.connection is not None:
            await self.connection.close()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self.connection is not None
        return self.connection

    async def known_channel(self, username: str) -> aiosqlite.Row | None:
        async with self.db.execute("SELECT * FROM channels WHERE username=?", (username,)) as cursor:
            return await cursor.fetchone()

    async def upsert_channel(self, channel: Channel, now: str) -> Channel:
        async with self.db.execute("SELECT folder FROM channels WHERE id = ?", (channel.id,)) as cursor:
            row = await cursor.fetchone()
        folder = row["folder"] if row else channel.folder
        if row is None:
            async with self.db.execute("SELECT id FROM channels WHERE folder=?", (folder,)) as cursor:
                collision = await cursor.fetchone()
            if collision is not None:
                folder = f"{folder}_{channel.id}"
        await self.db.execute(
            "INSERT INTO channels(id, username, folder, photo_id, access_hash, last_seen_utc) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET username=excluded.username, photo_id=excluded.photo_id, "
            "access_hash=excluded.access_hash,last_seen_utc=excluded.last_seen_utc",
            (channel.id, channel.username, folder, channel.photo_id, channel.access_hash, now),
        )
        await self.db.commit()
        return Channel(channel.id, channel.username, folder, channel.photo_id, channel.access_hash)

    async def group_anchor(self, channel_id: int, group_id: int, message_id: int, date_utc: str) -> tuple[int, str]:
        await self.db.execute(
            "INSERT OR IGNORE INTO media_groups(channel_id, group_id, anchor_message_id, date_utc) VALUES(?,?,?,?)",
            (channel_id, group_id, message_id, date_utc),
        )
        await self.db.commit()
        async with self.db.execute(
            "SELECT anchor_message_id, date_utc FROM media_groups WHERE channel_id=? AND group_id=?",
            (channel_id, group_id),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["anchor_message_id"]), str(row["date_utc"])

    async def upsert_message(self, channel_id: int, message_id: int, group_id: int | None, date_utc: str, url: str) -> None:
        await self.db.execute(
            "INSERT INTO messages(channel_id,message_id,group_id,publication_date_utc,post_url) VALUES(?,?,?,?,?) "
            "ON CONFLICT(channel_id,message_id) DO UPDATE SET group_id=excluded.group_id, "
            "publication_date_utc=excluded.publication_date_utc,post_url=excluded.post_url",
            (channel_id, message_id, group_id, date_utc, url),
        )
        await self.db.commit()

    async def upsert_media(self, item: MediaItem) -> aiosqlite.Row:
        await self.db.execute(
            "INSERT INTO media(channel_id,media_key,message_id,group_id,telegram_file_id,kind,source_name,mime_type,"
            "expected_size,original_path,enriched_path,publication_date_utc,download_date_utc,post_url) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(channel_id,media_key) DO UPDATE SET "
            "enriched_status=CASE WHEN media.post_url<>excluded.post_url THEN 'pending' ELSE media.enriched_status END, "
            "enriched_sha256=CASE WHEN media.post_url<>excluded.post_url THEN NULL ELSE media.enriched_sha256 END, "
            "post_url=excluded.post_url,publication_date_utc=excluded.publication_date_utc",
            (item.channel_id, item.key, item.message_id, item.group_id, item.file_id, item.kind,
             item.source_name, item.mime_type, item.expected_size, str(item.original_path),
             str(item.enriched_path), iso_utc(item.publication_date) if item.publication_date else None,
             iso_utc(item.download_date), item.post_url),
        )
        await self.db.commit()
        return await self.get_media(item.channel_id, item.key)

    async def get_media(self, channel_id: int, key: str) -> aiosqlite.Row:
        async with self.db.execute("SELECT * FROM media WHERE channel_id=? AND media_key=?", (channel_id, key)) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return row

    async def path_in_use(self, path: Path, key: str) -> bool:
        enriched = enriched_for_original(path)
        async with self.db.execute(
            "SELECT 1 FROM media WHERE (original_path=? OR enriched_path=?) AND media_key<>? LIMIT 1",
            (str(path), str(enriched), key),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def update_paths(self, item: MediaItem, original: Path, enriched: Path) -> None:
        await self.db.execute(
            "UPDATE media SET original_path=?, enriched_path=? WHERE channel_id=? AND media_key=?",
            (str(original), str(enriched), item.channel_id, item.key),
        )
        await self.db.commit()

    async def set_state(self, item: MediaItem, *, original: Status | None = None,
                        enriched: Status | None = None, downloaded_bytes: int | None = None,
                        actual_size: int | None = None, original_hash: str | None = None,
                        enriched_hash: str | None = None, error: str | None = None,
                        attempt_at: str | None = None) -> None:
        fields: list[str] = []
        values: list[object] = []
        for name, value in (("original_status", original), ("enriched_status", enriched),
                            ("downloaded_bytes", downloaded_bytes), ("actual_size", actual_size),
                            ("original_sha256", original_hash), ("enriched_sha256", enriched_hash),
                            ("last_error", error), ("last_attempt_utc", attempt_at)):
            if value is not None:
                fields.append(f"{name}=?")
                values.append(value.value if isinstance(value, Status) else value)
        if attempt_at is not None:
            fields.append("attempts=attempts+1")
        if not fields:
            return
        values.extend((item.channel_id, item.key))
        await self.db.execute(f"UPDATE media SET {', '.join(fields)} WHERE channel_id=? AND media_key=?", values)
        await self.db.commit()

    async def clear_error(self, item: MediaItem) -> None:
        await self.db.execute("UPDATE media SET last_error=NULL WHERE channel_id=? AND media_key=?", (item.channel_id, item.key))
        await self.db.commit()

    async def set_download_date(self, item: MediaItem, date_utc: str) -> None:
        await self.db.execute(
            "UPDATE media SET download_date_utc=? WHERE channel_id=? AND media_key=?",
            (date_utc, item.channel_id, item.key),
        )
        await self.db.commit()

    async def invalidate_enriched(self, item: MediaItem) -> None:
        await self.db.execute(
            "UPDATE media SET enriched_status='pending', enriched_sha256=NULL WHERE channel_id=? AND media_key=?",
            (item.channel_id, item.key),
        )
        await self.db.commit()

    async def mark_metadata_unavailable(self, item: MediaItem, reason: str) -> None:
        await self.db.execute(
            "UPDATE media SET enriched_status='unsupported_metadata', enriched_sha256=NULL, last_error=? "
            "WHERE channel_id=? AND media_key=?",
            (reason, item.channel_id, item.key),
        )
        await self.db.commit()
