from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    amount = size / 1024
    for unit in ("KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB"):
        if amount < 1024 or unit == "YB":
            rounded = f"{amount:.1f}"
            if rounded == "1024.0" and unit != "YB":
                amount = 1
                continue
            rounded = rounded.rstrip("0").rstrip(".")
            return f"{rounded} {unit}"
        amount /= 1024
    raise AssertionError("Unreachable size unit")


def safe_name(value: str, fallback: str = "file") -> str:
    value = Path(value.replace("\\", "/")).name
    value = re.sub(r"[\x00-\x1f\x7f<>:\"/\\|?*]", "_", value).strip(" .")
    return value[:180] or fallback


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def durable_flush(handle: object) -> None:
    handle.flush()
    os.fsync(handle.fileno())
