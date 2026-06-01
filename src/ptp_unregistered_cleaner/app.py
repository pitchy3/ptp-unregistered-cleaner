"""Application entry points for one-shot and daemon modes."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .config import Config, ConfigError, load_config, sanitized_config_summary
from .matcher import find_matches, remove_matches
from .ptp_client import PtpClient
from .qbittorrent_client import QBittorrentClient
from .state import save_state, successful_state

LOGGER = logging.getLogger(__name__)
DEFAULT_CONFIG_PATH = "/config/config.yaml"


def load_config_from_env() -> Config:
    path = os.environ.get("PTP_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    return load_config(Path(path))


def check_config() -> None:
    config = load_config_from_env()
    LOGGER.info("Configuration is valid: %s", sanitized_config_summary(config))


def run_once(config: Config | None = None) -> None:
    cfg = config or load_config_from_env()
    LOGGER.info(
        "Starting cleanup run: dry_run=%s max_deletes_per_run=%s instances=%s",
        cfg.app.dry_run,
        cfg.app.max_deletes_per_run,
        [instance.name for instance in cfg.qbittorrent],
    )
    ptp_torrents = PtpClient(cfg.ptp, cfg.credentials).fetch_unregistered()
    removed_by_instance: dict[str, list[str]] = {}
    skipped_state: list[dict[str, str]] = []

    for instance in cfg.qbittorrent:
        with QBittorrentClient(instance) as client:
            torrents = client.list_torrents()
            matches, filtered = find_matches(instance.name, torrents, ptp_torrents, cfg.matching)
            for torrent, reason in filtered:
                LOGGER.info("Skipping %s on %s: %s", torrent.hash, instance.name, reason)
                skipped_state.append(
                    {"instance": instance.name, "hash": torrent.hash, "reason": reason}
                )
            removed, skipped = remove_matches(
                client,
                matches,
                dry_run=cfg.app.dry_run,
                max_deletes_per_run=cfg.app.max_deletes_per_run,
                require_tracker_contains=cfg.matching.require_tracker_contains,
            )
            if removed:
                removed_by_instance.setdefault(instance.name, []).extend(
                    match.torrent.hash for match in removed
                )
            for match, reason in skipped:
                skipped_state.append(
                    {"instance": instance.name, "hash": match.torrent.hash, "reason": reason}
                )

    save_state(
        cfg.app.state_path,
        successful_state(
            infohash_count=len(ptp_torrents),
            removed_hashes_by_instance=removed_by_instance,
            skipped_hashes=skipped_state,
        ),
    )
    LOGGER.info("Cleanup run completed successfully")


def run_daemon(config: Config | None = None) -> None:
    cfg = config or load_config_from_env()
    interval_seconds = cfg.app.interval_days * 24 * 60 * 60
    if os.environ.get("RUN_ONCE", "").lower() in {"1", "true", "yes", "on"}:
        LOGGER.info("RUN_ONCE override is set; running once and exiting")
        run_once(cfg)
        return

    LOGGER.info("Starting daemon with interval_days=%s", cfg.app.interval_days)
    if cfg.app.run_on_startup:
        run_once(cfg)
    while True:
        LOGGER.info("Sleeping %.0f seconds until next cleanup run", interval_seconds)
        time.sleep(interval_seconds)
        try:
            run_once(cfg)
        except ConfigError:
            raise
        except Exception:
            LOGGER.exception("Cleanup run failed; daemon will retry after the configured interval")
