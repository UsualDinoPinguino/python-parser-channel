from __future__ import annotations

import logging
import re
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


class UtcFormatter(logging.Formatter):
    converter = time.gmtime


class SecretFilter(logging.Filter):
    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__()
        self.secrets = tuple(value for value in secrets if value)

    def add_secret(self, value: str) -> None:
        if value:
            self.secrets += (value,)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "[REDACTED]")
        message = re.sub(r"(?<!\w)\+?\d[\d ()-]{7,}\d(?!\w)", "[PHONE]", message)
        message = re.sub(r"(?<![a-fA-F0-9])[a-fA-F0-9]{32}(?![a-fA-F0-9])", "[HASH]", message)
        record.msg = message
        record.args = ()
        return True


def configure_logging(output: Path, secrets: tuple[str, ...]) -> tuple[logging.Logger, Path, SecretFilter]:
    log_path = output / "logs" / "download.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("channel_downloader")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(UtcFormatter("%(asctime)s UTC %(levelname)s %(module)s %(message)s", "%Y-%m-%dT%H:%M:%S"))
    redactor = SecretFilter(secrets)
    handler.addFilter(redactor)
    logger.addHandler(handler)
    logging.getLogger("telethon").setLevel(logging.CRITICAL)
    return logger, log_path, redactor
