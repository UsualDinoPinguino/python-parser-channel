# Telegram Public Channel Media Downloader

This command-line application downloads media from public Telegram broadcast channels through your **own Telegram user account**. It can download a channel's profile photo and supported media from posts, preserve the downloaded originals, and create separate copies with verified metadata when the source files contain embedded metadata.

The application runs once per command. It does not monitor channels continuously, download private channels, or save post text.

## Requirements

- Ubuntu with Python **3.12 or newer** and the `venv` module for that Python installation.
- A Telegram user account that can access the target public channel.
- Your own Telegram `api_id` and `api_hash`. These are **not** a bot token. Follow [Telegram's application setup instructions](https://core.telegram.org/api/obtaining_api_id): sign in at [my.telegram.org](https://my.telegram.org), open **API development tools**, and create an application.
- ExifTool for reading and writing image metadata.
- FFmpeg and FFprobe for reading and writing audio/video metadata.
- Enough disk space for downloads. A file with an enriched copy uses space for both the original and the copy.

On Ubuntu, install the metadata tools with:

```bash
sudo apt update
sudo apt install libimage-exiftool-perl ffmpeg
```

Ensure that Python 3.12 or newer is installed before continuing. The commands below use `python3.12`; replace that command with your installed Python 3.12+ executable if necessary.

## Installation

```bash
git clone https://github.com/UsualDinoPinguino/python-parser-channel.git
cd python-parser-channel
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Run the application from the repository root. The local `.env` and `.telegram.session` files are stored in the **current working directory**, so starting it from another directory will use different credentials and session data.

## First run and sign-in

Run a command such as:

```bash
python -m app download @nakedboots --limit 1
```

If `TELEGRAM_API_ID` or `TELEGRAM_API_HASH` is missing, the program asks for it in the terminal and saves the entered value in a local `.env` file. The API ID must be a positive integer, and the API hash must be a 32-character hexadecimal string. Run the first command in an interactive terminal if you want to use these prompts.

Alternatively, set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` in your environment or put them in `.env` yourself. A manually created `.env` uses this format; replace the example values with your own:

```dotenv
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
DOWNLOAD_CONCURRENCY=2
```

Run `chmod 600 .env` if you create the file yourself. Environment variables take precedence over `.env`. `DOWNLOAD_CONCURRENCY` is optional; it accepts an integer from 1 to 8 and defaults to 2.

On the first Telegram sign-in, enter your phone number, the code Telegram sends you, and your two-factor authentication password if your account uses one. The authorized session is saved locally in `.telegram.session`, so later commands normally do not require another sign-in. If Telegram invalidates the session, the program will ask you to sign in again.

Keep `.env` and `.telegram.session` private. They are ignored by Git, along with the `downloads/` directory.

## Download commands

Use a public channel username beginning with `@`, not a `t.me` URL:

```bash
# Download all accessible history.
python -m app download @channel

# Process the latest 100 posts.
python -m app download @channel --limit 100

# Process posts within an inclusive UTC date range.
python -m app download @chudo_photo --from-date 2023-01-11 --to-date 2023-02-17

# Process posts from a date through the latest available post.
python -m app download @channel --from-date 2026-01-01 --to-date now

# Choose a different download directory.
python -m app download @channel --limit 10 --output /path/to/archive
```

`--from-date` and `--to-date` can also be used separately. Dates use `YYYY-MM-DD`, are interpreted in UTC, and include the entire specified day. `now` is accepted for `--to-date`. Do not combine `--limit` with date options. The `--limit` value counts posts, including posts without downloadable media; an album counts as one post. The channel profile photo is checked separately from these post filters.

Run `python -m app download --help` to see the available options.

## What gets downloaded

Supported content includes channel profile photos, post photos, ordinary videos, video notes, voice messages, audio files, and documents. Albums are stored together in one post folder. GIF animations and stickers are skipped.

The downloader requests the largest available Telegram photo variant. A photo sent as a Telegram photo may already have been compressed and stripped of EXIF data by Telegram; the application cannot recover the pre-upload file or metadata. A photo sent as a document may retain its original metadata.

The default output layout is:

```text
downloads/
├── state.sqlite3
├── logs/
│   └── download.log
└── channel_name/
    ├── profile/
    │   ├── channel-...jpg
    │   └── enriched/              # Only when an enriched copy was created
    │       └── channel-...jpg
    └── posts/
        └── YYYY-MM-DD_message-id/
            ├── original-file.jpg
            └── enriched/          # Only when an enriched copy was created
                └── original-file.jpg
```

Original files are stored directly in the profile or post folder. The application creates an `enriched/` subfolder only when it can produce and verify an enriched copy. It does not create an empty `original/` folder.

The original bytes are never modified to add metadata. For files that already have meaningful embedded metadata, the program copies the original and adds the UTC publication date, UTC download date, and public post URL to the copy. For a profile photo, it adds only the download date and channel URL. Image metadata is written with ExifTool; audio and video metadata is written with FFmpeg without re-encoding when the container permits it. The program reads the metadata back to verify the result.

If a file has no embedded metadata, its format cannot be enriched safely, or a required metadata tool is unavailable, only the original is retained. The reason is recorded in `state.sqlite3` and `logs/download.log`. Install the missing tools and repeat the same command to retry files that do have embedded metadata.

## Resuming and checking results

`state.sqlite3` tracks media identities, paths, statuses, sizes, and SHA-256 hashes. On a repeated run, the application verifies existing files, skips valid ones, resumes a partial `.part` download when safe, repairs missing or corrupted originals, and recreates missing or corrupted enriched copies without re-downloading valid originals. Keep the SQLite file with the download directory if you want this behavior.

The terminal shows brief progress and a final summary. **Downloaded data** is the combined size of original files successfully downloaded during that run, displayed in readable units (for example, `13107200 B` appears as `12.5 MB`). Already valid files that were skipped do not add to this total. A nonzero exit status means a fatal error or at least one failed file; you can repeat the command after resolving the issue.

Detailed logs are written to `<output>/logs/download.log`. If an enriched copy is missing, check the `Unsupported metadata files` or `Failed files` counts and the log for the reason. In particular:

- `ExifTool is unavailable` means `libimage-exiftool-perl` is not installed or `exiftool` is not on `PATH`.
- `FFmpeg or FFprobe is unavailable` means `ffmpeg` is not installed or its commands are not on `PATH`.
- `No embedded image metadata was found` usually means Telegram removed the source metadata from a compressed photo; keeping only the original is expected.
- `The target must be an accessible public broadcast channel` means the account could not resolve an accessible public channel for that username. Private channels, groups, and inaccessible channels are outside the current scope.

The application respects Telegram rate limits and does not bypass access restrictions. Use it only for content your account is allowed to access.
