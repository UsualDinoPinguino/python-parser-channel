from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from .models import ConfigurationError


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    output: Path
    concurrency: int = 2
    max_attempts: int = 4


def load_config(output: Path) -> Config:
    local = dotenv_values(Path.cwd() / ".env")
    api_id_text = (os.environ["TELEGRAM_API_ID"] if "TELEGRAM_API_ID" in os.environ
                   else local.get("TELEGRAM_API_ID"))
    api_hash = (os.environ["TELEGRAM_API_HASH"] if "TELEGRAM_API_HASH" in os.environ
                else local.get("TELEGRAM_API_HASH"))
    if not api_id_text or not api_id_text.isdecimal() or int(api_id_text) <= 0:
        raise ConfigurationError("TELEGRAM_API_ID must be a positive integer.")
    if not api_hash or re.fullmatch(r"[a-fA-F0-9]{32}", api_hash) is None:
        raise ConfigurationError("TELEGRAM_API_HASH must be a 32-character hexadecimal value.")
    concurrency_text = (os.environ["DOWNLOAD_CONCURRENCY"] if "DOWNLOAD_CONCURRENCY" in os.environ
                        else local.get("DOWNLOAD_CONCURRENCY", "2"))
    if not concurrency_text or not concurrency_text.isdecimal() or not 1 <= int(concurrency_text) <= 8:
        raise ConfigurationError("DOWNLOAD_CONCURRENCY must be between 1 and 8.")
    return Config(int(api_id_text), api_hash, output.expanduser().resolve(), int(concurrency_text))
