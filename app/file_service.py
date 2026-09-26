from __future__ import annotations

import mimetypes
import os
import shutil
from datetime import timezone
from pathlib import Path

from telethon.tl import types

from .models import Channel, MediaItem
from .utils import safe_name, sha256_file, utc_now


EXTENSIONS = {
    "photo": ".jpg", "video": ".mp4", "video_note": ".mp4",
    "voice": ".ogg", "audio": ".mp3", "document": ".bin",
}


def photo_download_spec(photo: types.Photo) -> tuple[types.InputPhotoFileLocation | None, int | None]:
    candidates: list[tuple[int, str]] = []
    for size in photo.sizes:
        if isinstance(size, types.PhotoSize):
            candidates.append((size.size, size.type))
        elif isinstance(size, types.PhotoSizeProgressive) and size.sizes:
            candidates.append((max(size.sizes), size.type))
        elif isinstance(size, types.PhotoCachedSize):
            candidates.append((len(size.bytes), size.type))
    if not candidates:
        return None, None
    expected_size, thumb_size = max(candidates)
    location = types.InputPhotoFileLocation(
        id=photo.id, access_hash=photo.access_hash,
        file_reference=photo.file_reference, thumb_size=thumb_size,
    )
    return location, expected_size


def media_kind(message: types.Message) -> str | None:
    if message.photo:
        return "photo"
    if not message.document or not message.file:
        return None
    document = message.document
    attributes = document.attributes or []
    if (message.gif or message.sticker or any(isinstance(a, (types.DocumentAttributeAnimated, types.DocumentAttributeSticker)) for a in attributes)
            or (document.mime_type or "").lower() in {"image/gif", "application/x-tgsticker"}):
        return None
    if message.video_note:
        return "video_note"
    if message.voice:
        return "voice"
    if message.audio:
        return "audio"
    if message.video:
        return "video"
    mime = (document.mime_type or "").lower()
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "document"


def file_name(message: types.Message, kind: str) -> tuple[str, str | None]:
    source_name = message.file.name if message.file else None
    if source_name:
        cleaned = safe_name(source_name)
        if not Path(cleaned).suffix:
            mime_suffix = mimetypes.guess_extension(message.file.mime_type or "") or EXTENSIONS[kind]
            cleaned += mime_suffix
        return cleaned, source_name
    mime = message.file.mime_type if message.file else None
    suffix = mimetypes.guess_extension(mime or "") or (message.file.ext if message.file else None) or EXTENSIONS[kind]
    if not suffix.startswith(".") or len(suffix) > 12:
        suffix = EXTENSIONS[kind]
    stamp = message.date.astimezone(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    return f"{stamp}_msg-{message.id}_{kind}{suffix}", None


def unique_path(folder: Path, name: str, message_id: int | None) -> Path:
    def occupied(path: Path) -> bool:
        enriched = path.parent.parent / "enriched" / path.name
        return (path.exists() or part_path(path).exists() or enriched.exists() or part_path(enriched).exists())

    candidate = folder / name
    if not occupied(candidate):
        return candidate
    source = Path(name)
    suffix = f"_msg-{message_id}" if message_id is not None else "_profile"
    candidate = folder / f"{source.stem}{suffix}{source.suffix}"
    index = 2
    while occupied(candidate):
        candidate = folder / f"{source.stem}{suffix}_{index}{source.suffix}"
        index += 1
    return candidate


def make_item(channel: Channel, message: types.Message, kind: str, output: Path,
              anchor_id: int, anchor_date: str) -> MediaItem:
    name, source_name = file_name(message, kind)
    original_dir = output / channel.folder / "posts" / f"{anchor_date}_{anchor_id}" / "original"
    original_path = unique_path(original_dir, name, message.id)
    file_id = str(message.photo.id if message.photo else message.document.id)
    expected_size = (photo_download_spec(message.photo)[1] if message.photo
                     else message.file.size if message.file else None)
    group_id = int(message.grouped_id) if message.grouped_id is not None else None
    return MediaItem(
        key=f"message:{message.id}:{file_id}", channel_id=channel.id,
        message_id=message.id, group_id=group_id, file_id=file_id, kind=kind,
        source_name=source_name, mime_type=message.file.mime_type if message.file else None,
        expected_size=expected_size,
        publication_date=message.date, download_date=utc_now(),
        post_url=f"{channel.url}/{message.id}", original_path=original_path,
        enriched_path=original_path.parent.parent / "enriched" / original_path.name,
    )


def make_profile_item(channel: Channel, output: Path) -> MediaItem:
    now = utc_now()
    name = f"channel-{channel.id}_{now:%Y-%m-%d}_photo-{channel.photo_id}.jpg"
    original_path = output / channel.folder / "profile" / "original" / name
    return MediaItem(
        key=f"profile:{channel.photo_id}", channel_id=channel.id, message_id=None,
        group_id=None, file_id=str(channel.photo_id), kind="profile", source_name=None,
        mime_type="image/jpeg", expected_size=None, publication_date=None,
        download_date=now, post_url=channel.url, original_path=original_path,
        enriched_path=original_path.parent.parent / "enriched" / name,
    )


def valid_file(path: Path, expected_size: int | None, digest: str | None) -> bool:
    return bool(digest and path.is_file() and
                (expected_size is None or path.stat().st_size == expected_size) and
                sha256_file(path) == digest)


def part_path(path: Path) -> Path:
    return path.with_name(path.name + ".part")


def remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def copy_to_part(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = part_path(destination)
    remove_file(temporary)
    with source.open("rb") as input_file, temporary.open("xb") as output_file:
        shutil.copyfileobj(input_file, output_file, 1024 * 1024)
        output_file.flush()
        os.fsync(output_file.fileno())
    return temporary
