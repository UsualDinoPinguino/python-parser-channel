from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

from .config import load_config
from .database import Database
from .download_service import DownloadService
from .logging_config import configure_logging
from .metadata_service import MetadataService
from .models import AppError, ConfigurationError, Summary
from .telegram_service import TelegramService
from .utils import iso_utc, utc_now


def positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("limit must be a positive integer") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("limit must be a positive integer")
    return number


def utc_date(value: str) -> date:
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from None


def utc_end_date(value: str) -> date | str:
    return "now" if value.lower() == "now" else utc_date(value)


def parser() -> argparse.ArgumentParser:
    command_line = argparse.ArgumentParser(prog="python -m app")
    commands = command_line.add_subparsers(dest="command", required=True)
    download = commands.add_parser("download", help="Download media from a public channel")
    download.add_argument("channel", help="Public channel username, such as @channel")
    download.add_argument("--limit", type=positive_integer)
    download.add_argument("--from-date", type=utc_date)
    download.add_argument("--to-date", type=utc_end_date)
    download.add_argument("--output", type=Path, default=Path("downloads"))
    return command_line


def validate(arguments: argparse.Namespace, command_line: argparse.ArgumentParser) -> None:
    if not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", arguments.channel):
        command_line.error("channel must be a public @username")
    if arguments.limit is not None and (arguments.from_date is not None or arguments.to_date is not None):
        command_line.error("--limit cannot be combined with date options")
    if arguments.from_date and isinstance(arguments.to_date, date) and arguments.from_date > arguments.to_date:
        command_line.error("--from-date must not be after --to-date")


def print_summary(summary: Summary, output: Path, log_path: Path) -> None:
    print(f"Channel: {summary.channel}")
    print(f"Processed posts: {summary.processed_posts}")
    print(f"Downloaded originals: {summary.downloaded_originals}")
    print(f"Created enriched copies: {summary.created_enriched_copies}")
    print(f"Skipped valid files: {summary.skipped_valid_files}")
    print(f"Resumed downloads: {summary.resumed_downloads}")
    print(f"Repaired corrupted files: {summary.repaired_corrupted_files}")
    print(f"Unsupported metadata files: {summary.unsupported_metadata_files}")
    print(f"Failed files: {summary.failed_files}")
    print(f"Output directory: {output}")
    print(f"Log file: {log_path}")


async def run(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.output)
    logger, log_path, redactor = configure_logging(config.output, (str(config.api_id), config.api_hash))
    from_date = (datetime.combine(arguments.from_date, time.min, timezone.utc)
                 if arguments.from_date else None)
    to_date = (datetime.combine(arguments.to_date, time.max, timezone.utc)
               if isinstance(arguments.to_date, date) else None)
    try:
        async with Database(config.output / "state.sqlite3") as database:
            known = await database.known_channel(arguments.channel[1:])
            async with TelegramService(config, logger, redactor) as telegram:
                print("Resolving channel...")
                channel, entity = await telegram.resolve_channel(arguments.channel[1:], known)
                channel = await database.upsert_channel(channel, iso_utc(utc_now()))
                downloader = DownloadService(config, database, telegram, MetadataService(), logger)
                summary = await downloader.run(channel, entity, arguments.limit, from_date, to_date)
        print_summary(summary, config.output, log_path)
        return 0 if summary.failed_files == 0 else 1
    except AppError as exc:
        logger.error("Fatal error: %s", type(exc).__name__)
        print(f"Error: {exc}", file=sys.stderr)
        print(f"Log file: {log_path}", file=sys.stderr)
        return 1
    except Exception as exc:
        logger.error("Unexpected fatal error: %s", type(exc).__name__)
        print("Error: The operation could not be completed. See the log file.", file=sys.stderr)
        print(f"Log file: {log_path}", file=sys.stderr)
        return 1


def main() -> int:
    command_line = parser()
    arguments = command_line.parse_args()
    validate(arguments, command_line)
    try:
        return asyncio.run(run(arguments))
    except ConfigurationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Partial downloads can be resumed on the next run.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
