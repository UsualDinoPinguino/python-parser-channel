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
