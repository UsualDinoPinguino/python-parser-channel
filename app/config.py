from __future__ import annotations

import os
import re
import sys
from getpass import getpass
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
    env_path = Path.cwd() / ".env"
    local = dotenv_values(env_path)
    api_id_text = (os.environ["TELEGRAM_API_ID"] if "TELEGRAM_API_ID" in os.environ
                   else local.get("TELEGRAM_API_ID"))
    api_hash = (os.environ["TELEGRAM_API_HASH"] if "TELEGRAM_API_HASH" in os.environ
                else local.get("TELEGRAM_API_HASH"))
    prompted: dict[str, str] = {}
    if not api_id_text or not api_hash:
        if not sys.stdin.isatty():
            raise ConfigurationError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required in an interactive terminal or environment.")
        try:
            if not api_id_text:
                api_id_text = getpass("TELEGRAM_API_ID: ").strip()
                prompted["TELEGRAM_API_ID"] = api_id_text
            if not api_hash:
                api_hash = getpass("TELEGRAM_API_HASH: ").strip()
                prompted["TELEGRAM_API_HASH"] = api_hash
        except EOFError:
            raise ConfigurationError("API credential entry was cancelled.") from None
    if not api_id_text or not api_id_text.isdecimal() or int(api_id_text) <= 0:
        raise ConfigurationError("TELEGRAM_API_ID must be a positive integer.")
    if not api_hash or re.fullmatch(r"[a-fA-F0-9]{32}", api_hash) is None:
        raise ConfigurationError("TELEGRAM_API_HASH must be a 32-character hexadecimal value.")
    if prompted:
        _save_credentials(env_path, prompted)
    concurrency_text = (os.environ["DOWNLOAD_CONCURRENCY"] if "DOWNLOAD_CONCURRENCY" in os.environ
                        else local.get("DOWNLOAD_CONCURRENCY", "2"))
    if not concurrency_text or not concurrency_text.isdecimal() or not 1 <= int(concurrency_text) <= 8:
        raise ConfigurationError("DOWNLOAD_CONCURRENCY must be between 1 and 8.")
    return Config(int(api_id_text), api_hash, output.expanduser().resolve(), int(concurrency_text))


def _save_credentials(env_path: Path, values: dict[str, str]) -> None:
    try:
        existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        suffix = "" if not existing or existing.endswith("\n") else "\n"
        content = existing + suffix + "".join(f"{key}={value}\n" for key, value in values.items())
        temporary = env_path.with_name(f"{env_path.name}.tmp-{os.getpid()}")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, env_path)
            env_path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError:
        raise ConfigurationError("API credentials could not be saved to the local .env file.") from None
