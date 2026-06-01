# ptp-unregistered-cleaner

`ptp-unregistered-cleaner` is a small containerized Python daemon/CLI that compares PassThePopcorn (PTP) unregistered torrent infohashes with one or more qBittorrent Web API instances and removes matching torrent entries from qBittorrent **without deleting downloaded files**.

## What this does

- Queries the PTP unregistered-history JSON endpoint:
  `https://passthepopcorn.me/userhistory.php?action=unregistered&type=json`
- Reads `ApiUser` and `ApiKey` from environment variables.
- Extracts, lowercases, and deduplicates returned `InfoHash` values.
- Supports paginated PTP responses using a configurable page parameter.
- Checks all configured qBittorrent instances for matching torrent hashes.
- Optionally verifies that each matching torrent has a tracker URL containing a configured substring, which defaults to `passthepopcorn`.
- Removes matching qBittorrent torrent entries with `deleteFiles=false`.
- Maintains a best-effort JSON state file with the most recent run summary.

## What this does NOT do

- Does not download torrents.
- Does not delete movie files or downloaded data.
- Does not scrape torrent pages.
- Does not rapidly poll PTP; it is designed for low-frequency use every few days.
- Does not store PTP or qBittorrent secrets in committed configuration.

## Safety / acceptable use

- Designed for low-frequency personal maintenance.
- Default configuration uses `dry_run: true`.
- Respect PTP API rules and avoid aggressive polling.
- Keep `ApiUser` and `ApiKey` private.
- Review logs before posting them publicly. The application avoids logging configured secrets, but torrent names, hashes, and PTP metadata may still be sensitive.
- Keep `max_deletes_per_run` set to a conservative value. Live mode will not delete beyond this cap in one run.

## Quick start

1. Copy environment template:

   ```bash
   cp .env.example .env
   ```

2. Edit `.env` and fill in:

   ```dotenv
   PTP_API_USER=
   PTP_API_KEY=
   QBIT_MAIN_USERNAME=
   QBIT_MAIN_PASSWORD=
   ```

3. Copy config template:

   ```bash
   cp config.example.yaml config.yaml
   ```

4. Edit `config.yaml` for your qBittorrent instance URLs and environment variable names. Start with:

   ```yaml
   app:
     dry_run: true
   ```

5. Run with Docker Compose:

   ```bash
   docker compose -f docker-compose.example.yml up --build
   ```

6. Review dry-run logs. Only set `dry_run: false` after confirming matches are correct.

## Config reference

Configuration is loaded from `PTP_CONFIG_PATH`, defaulting to `/config/config.yaml` in Docker.

### `app`

| Key | Default | Description |
| --- | --- | --- |
| `interval_days` | `3` | Days between daemon runs. Must be greater than zero. |
| `run_on_startup` | `true` | Run immediately when daemon mode starts. |
| `dry_run` | `true` | If true, log what would be removed but do not call qBittorrent delete. |
| `max_deletes_per_run` | `25` | Safety cap for live removals per qBittorrent instance run path. |
| `state_path` | `/data/state.json` | Best-effort JSON state file path. |

### `ptp`

| Key | Default | Description |
| --- | --- | --- |
| `base_url` | `https://passthepopcorn.me` | PTP base URL. |
| `unregistered_path` | `/userhistory.php` | API path. |
| `timeout_seconds` | `30` | HTTP timeout. |
| `min_interval_seconds_between_pages` | `5` | Sleep between paginated PTP requests. |
| `page_parameter` | `page` | Name of pagination query parameter. Change this if the API expects a different name. |

### `matching`

| Key | Default | Description |
| --- | --- | --- |
| `require_tracker_contains` | `passthepopcorn` | Require at least one qBittorrent tracker URL to contain this substring. Set to `""` to disable. |
| `include_categories` | `[]` | Empty means include all categories. If set, category must match one entry. |
| `exclude_categories` | `[]` | Categories that are always skipped. Excludes win. |
| `include_tags` | `[]` | Empty means include all tags. If set, torrent must have at least one listed tag. |
| `exclude_tags` | `[]` | Tags that are always skipped. Excludes win. |

### `qbittorrent`

A list of qBittorrent Web API instances:

```yaml
qbittorrent:
  - name: main
    url: http://qbittorrent:8080
    username: ${QBIT_MAIN_USERNAME}
    password: ${QBIT_MAIN_PASSWORD}
```

The loader supports simple `${VAR_NAME}` interpolation. If a referenced variable is missing, startup fails with an error naming that variable.

## How matching works

1. PTP `InfoHash` values are normalized to lowercase and deduplicated.
2. qBittorrent torrent `hash` values are normalized to lowercase.
3. A qBittorrent torrent is a candidate when its normalized hash exists in the PTP unregistered set.
4. Category and tag filters are applied:
   - Empty include lists include everything.
   - Exclude lists always win.
5. If `require_tracker_contains` is non-empty, the qBittorrent tracker list for the torrent must contain that substring case-insensitively.

Candidate logs include only safer operational fields: qBittorrent instance name, torrent name, torrent hash, PTP `FileName`, PTP `TorrentID`, PTP `ReasonText`, and PTP `DeletedTime`.

## How deletion works

In dry-run mode, the app logs `DRY RUN: would remove ...` and does not call the qBittorrent delete endpoint.

In live mode, each matched torrent is removed with the qBittorrent Web API delete call using:

```text
deleteFiles=false
```

This removes the torrent entry from qBittorrent but does **not** delete downloaded files. This project never enables qBittorrent file deletion.

## Running one-shot

With a local Python environment:

```bash
PTP_CONFIG_PATH=./config.yaml ptp-unregistered-cleaner run-once
```

With Docker Compose, set this in `.env` for a one-shot daemon entrypoint override:

```dotenv
RUN_ONCE=true
```

Then run the compose file normally.

## Running as daemon

```bash
PTP_CONFIG_PATH=./config.yaml ptp-unregistered-cleaner daemon
```

Docker defaults to daemon mode:

```bash
docker compose -f docker-compose.example.yml up -d --build
```

The daemon sleeps for `interval_days` between runs. Do not set this to a rapid polling interval.

## CLI commands

```bash
ptp-unregistered-cleaner check-config
ptp-unregistered-cleaner run-once
ptp-unregistered-cleaner daemon
```

## Troubleshooting

### PTP 400

HTTP 400 may indicate malformed API credentials. Confirm `PTP_API_USER` and `PTP_API_KEY` are populated correctly in `.env` and passed into the container.

### PTP 401

HTTP 401 may indicate API privileges are disabled. Confirm the account/API settings permit API access.

### PTP 302

PTP 302 means the request was redirected, commonly because API credentials were rejected, API privileges are disabled, or the request was not authenticated as an API request. Confirm `PTP_API_USER`, `PTP_API_KEY`, and the account/API settings permit API access. This app sends PTP credentials as HTTP headers (`ApiUser` and `ApiKey`), not query parameters.

### qBittorrent auth failure

Verify the qBittorrent Web UI URL, username, and password. Make sure the username and password are provided via environment variables and interpolated into `config.yaml`.

### Tracker verification skipped or failed

If `matching.require_tracker_contains` is empty, tracker verification is disabled. If it is set and removals are skipped, confirm the torrent has a tracker URL containing that substring.

### Dry-run shows matches but live run deletes none

Check:

- `app.dry_run` is set to `false` in the mounted `config.yaml`.
- Tracker verification is passing.
- Category/tag filters are not excluding the torrents.
- `max_deletes_per_run` is greater than zero.
- The container was restarted after config changes.

## Development

Install development dependencies and run checks:

```bash
python -m pip install -e '.[dev]'
pytest
ruff check .
```
