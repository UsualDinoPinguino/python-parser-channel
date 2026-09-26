from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path


class AppError(Exception):
    """A safe, user-facing application error."""


class ConfigurationError(AppError):
    pass


class AuthenticationError(AppError):
    pass


class ChannelError(AppError):
    pass


class DownloadError(AppError):
    pass


class MetadataError(AppError):
    pass


class UnsupportedMetadata(MetadataError):
    pass


class Status(str, Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    ENRICHING = "enriching"
    COMPLETE = "complete"
    UNSUPPORTED_METADATA = "unsupported_metadata"
    FAILED = "failed"
    CORRUPTED = "corrupted"


@dataclass(frozen=True)
class Channel:
    id: int
    username: str
    folder: str
    photo_id: int | None
    access_hash: int

    @property
    def url(self) -> str:
        return f"https://t.me/{self.username}"


@dataclass(frozen=True)
class MediaItem:
    key: str
    channel_id: int
    message_id: int | None
    group_id: int | None
    file_id: str
    kind: str
    source_name: str | None
    mime_type: str | None
    expected_size: int | None
    publication_date: datetime | None
    download_date: datetime
    post_url: str
    original_path: Path
    enriched_path: Path


@dataclass
class Summary:
    channel: str
    processed_posts: int = 0
    downloaded_originals: int = 0
    created_enriched_copies: int = 0
    skipped_valid_files: int = 0
    resumed_downloads: int = 0
    repaired_corrupted_files: int = 0
    unsupported_metadata_files: int = 0
    failed_files: int = 0
