from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .models import MediaItem, MetadataError, UnsupportedMetadata
from .utils import iso_utc


IMAGE_FORMATS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
STREAM_FORMATS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".mp3", ".m4a", ".ogg", ".opus", ".flac"}


async def run_command(*arguments: str) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
    except OSError:
        raise MetadataError("Metadata tool could not be started.") from None
    if process.returncode != 0:
        raise MetadataError("Metadata tool rejected the file.")
    return stdout


def same_utc(value: str, expected: str) -> bool:
    try:
        normalized = value.replace("Z", "+00:00")
        if len(normalized) > 10 and normalized[4] == ":":
            normalized = normalized[:4] + "-" + normalized[5:7] + "-" + normalized[8:]
        if len(normalized) > 10 and normalized[10] == " ":
            normalized = normalized[:10] + "T" + normalized[11:]
        return datetime.fromisoformat(normalized).astimezone(timezone.utc) == datetime.fromisoformat(expected.replace("Z", "+00:00"))
    except ValueError:
        return False


class MetadataService:
    async def has_embedded_metadata(self, path: Path, item: MediaItem) -> tuple[bool, str]:
        extension = item.original_path.suffix.lower()
        if extension in IMAGE_FORMATS:
            if not shutil.which("exiftool"):
                return False, "ExifTool is unavailable."
            try:
                tags = await self._image_tags(path)
            except MetadataError:
                return False, "ExifTool could not read embedded metadata."
            meaningful = any(key.startswith(("EXIF:", "IFD0:", "IFD1:", "ExifIFD:",
                                             "GPS:", "InteropIFD:", "MakerNotes:", "XMP-", "IPTC:"))
                             for key in tags)
            return meaningful, "" if meaningful else "No embedded image metadata was found."
        if extension in STREAM_FORMATS:
            if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
                return False, "FFmpeg or FFprobe is unavailable."
            try:
                tags = await self._stream_tags(path)
            except MetadataError:
                return False, "FFprobe could not read embedded metadata."
            technical = {"encoder", "major_brand", "minor_version", "compatible_brands"}
            meaningful = any(key not in technical for key in tags)
            return meaningful, "" if meaningful else "No embedded stream metadata was found."
        return False, "No verified metadata reader is available for this format."

    async def enrich(self, temporary: Path, item: MediaItem) -> None:
        extension = item.original_path.suffix.lower()
        if extension in IMAGE_FORMATS:
            if not shutil.which("exiftool"):
                raise UnsupportedMetadata("ExifTool is unavailable.")
            await self._image(temporary, item)
        elif extension in STREAM_FORMATS:
            if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
                raise UnsupportedMetadata("FFmpeg or FFprobe is unavailable.")
            await self._stream(temporary, item)
        else:
            raise UnsupportedMetadata(f"No verified metadata writer for {extension or 'unknown format'}.")

    async def _image(self, temporary: Path, item: MediaItem) -> None:
        before = await self._image_tags(temporary)
        expected: dict[str, str] = {
            "XMP-xmp:MetadataDate": iso_utc(item.download_date),
            "XMP-dc:Source": item.post_url,
        }
        if item.publication_date is not None:
            expected["XMP-xmp:CreateDate"] = iso_utc(item.publication_date)
        arguments = ["exiftool", "-overwrite_original", "-P"]
        arguments.extend(f"-{key}={value}" for key, value in expected.items())
        arguments.append(str(temporary))
        await run_command(*arguments)
        data = await self._image_tags(temporary)
        for key, value in expected.items():
            actual = data.get(key)
            if actual is None or ("Date" in key and not same_utc(str(actual), value)) or ("Date" not in key and actual != value):
                raise MetadataError("Written image metadata did not pass verification.")
        for key, value in before.items():
            if key.startswith(("EXIF:", "XMP-", "IPTC:")) and key not in expected and data.get(key) != value:
                raise UnsupportedMetadata("Image writer changed existing metadata.")

    async def _image_tags(self, path: Path) -> dict[str, object]:
        output = await run_command("exiftool", "-json", "-G1", "-s", str(path))
        try:
            return json.loads(output)[0]
        except (ValueError, IndexError, TypeError):
            raise MetadataError("ExifTool verification returned invalid data.") from None

    async def _stream(self, temporary: Path, item: MediaItem) -> None:
        input_tags = await self._stream_tags(temporary)
        prior_comment = str(input_tags.get("comment", ""))
        lines = [prior_comment] if prior_comment else []
        if item.publication_date is not None:
            lines.append(f"Publication date UTC: {iso_utc(item.publication_date)}")
        lines.append(f"Download date UTC: {iso_utc(item.download_date)}")
        lines.append(f"Source URL: {item.post_url}")
        comment = "\n".join(lines)
        staged = temporary.with_name(temporary.name + ".metadata" + item.original_path.suffix)
        arguments = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(temporary),
                     "-map", "0", "-map_metadata", "0", "-c", "copy", "-metadata", f"comment={comment}"]
        if item.original_path.suffix.lower() in {".mp4", ".m4v", ".mov", ".m4a"}:
            arguments.extend(["-movflags", "+use_metadata_tags"])
        arguments.append(str(staged))
        try:
            await run_command(*arguments)
            tags = await self._stream_tags(staged)
            actual = str(tags.get("comment", ""))
            for line in lines:
                if line not in actual:
                    raise MetadataError("Written stream metadata did not pass verification.")
            volatile = {"comment", "encoder", "major_brand", "minor_version", "compatible_brands"}
            for key, value in input_tags.items():
                if key not in volatile and tags.get(key) != value:
                    raise UnsupportedMetadata("Stream writer changed existing metadata.")
            staged.replace(temporary)
        finally:
            staged.unlink(missing_ok=True)

    async def _stream_tags(self, path: Path) -> dict[str, object]:
        output = await run_command(
            "ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(path),
        )
        try:
            tags = json.loads(output).get("format", {}).get("tags", {})
            return {str(key).lower(): value for key, value in tags.items()}
        except (ValueError, AttributeError):
            raise MetadataError("FFprobe verification returned invalid data.") from None
