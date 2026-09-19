"""Application entry points for one-shot and daemon modes."""

from __future__ import annotations

import logging
import os
import time
from contextlib import ExitStack
from pathlib import Path

from .config import Config, ConfigError, load_config, sanitized_config_summary
from .matcher import Match, find_matches, remove_matches, tracker_verified
from .ptp_client import PtpClient
from .qbittorrent_client import QBittorrentClient, QBittorrentClientError
from .radarr_client import RadarrClient
from .replacement import ReplacementCoordinator
from .state import State, load_state, save_state, successful_state
from .torznab import ReplacementCatalog, ReplacementServer

LOGGER = logging.getLogger(__name__)
DEFAULT_CONFIG_PATH = "/config/config.yaml"


def load_config_from_env() -> Config:
    path = os.environ.get("PTP_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    return load_config(Path(path))


def check_config() -> None:
    config = load_config_from_env()
    LOGGER.info("Configuration is valid: %s", sanitized_config_summary(config))


def run_once(
    config: Config | None = None,
    *,
    ptp_client: PtpClient | None = None,
    coordinator: ReplacementCoordinator | None = None,
) -> None:
    cfg = config or load_config_from_env()
    LOGGER.info(
        "Starting cleanup run: dry_run=%s max_deletes_per_run=%s instances=%s",
        cfg.app.dry_run,
        cfg.app.max_deletes_per_run,
        [instance.name for instance in cfg.qbittorrent],
    )
    ptp_client = ptp_client or PtpClient(cfg.ptp, cfg.credentials)
    ptp_torrents = ptp_client.fetch_unregistered()
    removed_by_instance: dict[str, list[str]] = {}
    skipped_state: list[dict[str, str]] = []
    previous_state = load_state(cfg.app.state_path)
    replacement_requests = dict(previous_state.replacement_requests)

    with ExitStack() as stack:
        active_coordinator = coordinator
        if cfg.radarr.enabled and active_coordinator is None:
            catalog = ReplacementCatalog()
            server = ReplacementServer(
                cfg.radarr.torznab_host,
                cfg.radarr.torznab_port,
                cfg.radarr.torznab_external_url,
                cfg.radarr.torznab_api_key,
                catalog,
                ptp_client,
            )
            stack.enter_context(server)
            active_coordinator = ReplacementCoordinator(
                ptp_client, RadarrClient(cfg.radarr), catalog
            )
            LOGGER.info("Replacement Torznab server started on port %s", cfg.radarr.torznab_port)

        for instance in cfg.qbittorrent:
            removed: list[Match] = []
            skipped: list[tuple[Match, str]] = []
            try:
                with QBittorrentClient(instance) as client:
                    torrents = client.list_torrents()
                    matches, filtered = find_matches(
                        instance.name, torrents, ptp_torrents, cfg.matching
                    )
                    for torrent, reason in filtered:
                        LOGGER.info("Skipping %s on %s: %s", torrent.hash, instance.name, reason)
                        skipped_state.append(
                            {"instance": instance.name, "hash": torrent.hash, "reason": reason}
                        )
                    requests_before = dict(replacement_requests)
                    matches = _request_replacements(
                        matches,
                        active_coordinator,
                        client,
                        cfg.matching.require_tracker_contains,
                        cfg.app.dry_run,
                        cfg.radarr.preserve_on_failure,
                        replacement_requests,
                        skipped,
                    )
                    # Checkpoint successful grabs before qBittorrent removal. If removal
                    # fails, the next run can retry cleanup without downloading twice.
                    if replacement_requests != requests_before:
                        save_state(
                            cfg.app.state_path,
                            State(replacement_requests=replacement_requests),
                        )
                    remove_matches(
                        client,
                        matches,
                        dry_run=cfg.app.dry_run,
                        max_deletes_per_run=cfg.app.max_deletes_per_run,
                        require_tracker_contains=cfg.matching.require_tracker_contains,
                        removed_results=removed,
                        skipped_results=skipped,
                    )
            except QBittorrentClientError:
                LOGGER.exception(
                    "qBittorrent instance %s failed; skipping this instance", instance.name
                )
            if removed:
                removed_by_instance.setdefault(instance.name, []).extend(
                    match.torrent.hash for match in removed
                )
                for match in removed:
                    replacement_requests.pop(match.torrent.hash.strip().lower(), None)
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
            replacement_requests=replacement_requests,
        ),
    )
    LOGGER.info("Cleanup run completed successfully")


def _request_replacements(
    matches: list[Match],
    coordinator: ReplacementCoordinator | None,
    qbit_client: object,
    require_tracker_contains: str,
    dry_run: bool,
    preserve_on_failure: bool,
    replacement_requests: dict[str, str],
    skipped: list[tuple[Match, str]],
) -> list[Match]:
    if coordinator is None:
        return matches
    removable: list[Match] = []
    for match in matches:
        replacement_id = match.ptp.replacement_torrent_id
        if replacement_id is None:
            removable.append(match)
            continue
        if not tracker_verified(qbit_client, match.torrent.hash, require_tracker_contains):
            # remove_matches will record the normal tracker-verification skip.
            removable.append(match)
            continue
        if dry_run:
            LOGGER.info(
                "DRY RUN: would request PTP replacement torrent %s through Radarr before cleanup",
                replacement_id,
            )
            removable.append(match)
            continue
        old_hash = match.torrent.hash.strip().lower()
        if replacement_requests.get(old_hash) == replacement_id:
            LOGGER.info(
                "Replacement %s was already requested for %s; continuing cleanup",
                replacement_id,
                old_hash,
            )
            removable.append(match)
            continue
        try:
            coordinator.replace(match)
            replacement_requests[old_hash] = replacement_id
        except Exception as exc:
            LOGGER.exception(
                "Unable to request replacement %s for %s", replacement_id, match.torrent.hash
            )
            if preserve_on_failure:
                skipped.append((match, f"replacement failed; preserved for retry: {exc}"))
                continue
        removable.append(match)
    return removable


def run_daemon(config: Config | None = None) -> None:
    cfg = config or load_config_from_env()
    interval_seconds = cfg.app.interval_days * 24 * 60 * 60
    if os.environ.get("RUN_ONCE", "").lower() in {"1", "true", "yes", "on"}:
        LOGGER.info("RUN_ONCE override is set; running once and exiting")
        run_once(cfg)
        return

    LOGGER.info("Starting daemon with interval_days=%s", cfg.app.interval_days)
    with ExitStack() as stack:
        ptp_client = None
        coordinator = None
        if cfg.radarr.enabled:
            ptp_client = PtpClient(cfg.ptp, cfg.credentials)
            catalog = ReplacementCatalog()
            server = ReplacementServer(
                cfg.radarr.torznab_host,
                cfg.radarr.torznab_port,
                cfg.radarr.torznab_external_url,
                cfg.radarr.torznab_api_key,
                catalog,
                ptp_client,
            )
            stack.enter_context(server)
            coordinator = ReplacementCoordinator(
                ptp_client, RadarrClient(cfg.radarr), catalog
            )
            LOGGER.info(
                "Replacement Torznab server listening continuously on port %s",
                cfg.radarr.torznab_port,
            )
        if cfg.app.run_on_startup:
            _run_daemon_cleanup(cfg, ptp_client, coordinator)
        while True:
            LOGGER.info("Sleeping %.0f seconds until next cleanup run", interval_seconds)
            time.sleep(interval_seconds)
            _run_daemon_cleanup(cfg, ptp_client, coordinator)


def _run_daemon_cleanup(
    config: Config,
    ptp_client: PtpClient | None = None,
    coordinator: ReplacementCoordinator | None = None,
) -> None:
    try:
        if ptp_client is None and coordinator is None:
            run_once(config)
        else:
            run_once(config, ptp_client=ptp_client, coordinator=coordinator)
    except ConfigError:
        raise
    except Exception:
        LOGGER.exception("Cleanup run failed; daemon will retry after the configured interval")
