"""Application entry points for one-shot and daemon modes."""

from __future__ import annotations

import logging
import os
import time
from collections import Counter
from contextlib import ExitStack
from pathlib import Path

from .config import (
    Config,
    ConfigError,
    RadarrConfig,
    load_config,
    radarr_route,
    sanitized_config_summary,
)
from .matcher import Match, find_matches, remove_matches, tracker_verified
from .ptp_client import PtpClient
from .qbittorrent_client import QBittorrentClient, QBittorrentClientError
from .radarr_client import RadarrClient
from .replacement import ReplacementCoordinator
from .state import State, StateError, load_state, save_state, successful_state
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
    coordinators: dict[str, ReplacementCoordinator] | None = None,
    volatile_replacement_requests: dict[str, str] | None = None,
) -> None:
    cfg = config or load_config_from_env()
    LOGGER.info(
        "Starting cleanup run: dry_run=%s max_deletes_per_run=%s instances=%s",
        cfg.app.dry_run,
        cfg.app.max_deletes_per_run,
        [instance.name for instance in cfg.qbittorrent],
    )
    # Existing replacement checkpoints are a deletion/download safety boundary.
    # Load them before making any external request and abort if they are unreadable.
    previous_state = load_state(cfg.app.state_path)
    ptp_client = ptp_client or PtpClient(cfg.ptp, cfg.credentials)
    ptp_torrents = ptp_client.fetch_unregistered()
    removed_by_instance: dict[str, list[str]] = {}
    skipped_state: list[dict[str, str]] = []
    volatile_requests = (
        volatile_replacement_requests
        if volatile_replacement_requests is not None
        else {}
    )
    replacement_requests = dict(previous_state.replacement_requests)
    replacement_requests.update(volatile_requests)
    unpersisted_checkpoints = set(volatile_requests)
    remaining_torrent_copies: Counter[str] = Counter()
    all_instances_processed = True

    with ExitStack() as stack:
        active_coordinators = coordinators
        if cfg.radarr and active_coordinators is None:
            active_coordinators = _start_replacement_services(stack, cfg, ptp_client)
        active_coordinators = active_coordinators or {}

        for instance in cfg.qbittorrent:
            removed: list[Match] = []
            skipped: list[tuple[Match, str]] = []
            try:
                with QBittorrentClient(instance) as client:
                    torrents = client.list_torrents()
                    for torrent in torrents:
                        route = radarr_route(cfg.radarr, instance.name, torrent.category)
                        if route:
                            remaining_torrent_copies[
                                _checkpoint_key(route, torrent.hash)
                            ] += 1
                    matches, filtered = find_matches(
                        instance.name, torrents, ptp_torrents, cfg.matching
                    )
                    for torrent, reason in filtered:
                        LOGGER.info("Skipping %s on %s: %s", torrent.hash, instance.name, reason)
                        skipped_state.append(
                            {"instance": instance.name, "hash": torrent.hash, "reason": reason}
                        )
                    matches = _request_replacements(
                        matches,
                        cfg.radarr,
                        active_coordinators,
                        client,
                        cfg.matching.require_tracker_contains,
                        cfg.app.dry_run,
                        cfg.app.max_deletes_per_run,
                        replacement_requests,
                        skipped,
                        unpersisted_checkpoints,
                        cfg.app.state_path,
                        volatile_requests,
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
                all_instances_processed = False
                LOGGER.exception(
                    "qBittorrent instance %s failed; skipping this instance", instance.name
                )
            if removed:
                removed_by_instance.setdefault(instance.name, []).extend(
                    match.torrent.hash for match in removed
                )
                for match in removed:
                    route = radarr_route(
                        cfg.radarr, instance.name, match.torrent.category
                    )
                    if route:
                        remaining_torrent_copies[
                            _checkpoint_key(route, match.torrent.hash)
                        ] -= 1
            for match, reason in skipped:
                skipped_state.append(
                    {"instance": instance.name, "hash": match.torrent.hash, "reason": reason}
                )

        _prune_replacement_requests(
            replacement_requests, remaining_torrent_copies, all_instances_processed
        )
        for checkpoint in list(volatile_requests):
            if checkpoint not in replacement_requests:
                volatile_requests.pop(checkpoint, None)

    final_state_saved = save_state(
        cfg.app.state_path,
        successful_state(
            infohash_count=len(ptp_torrents),
            removed_hashes_by_instance=removed_by_instance,
            skipped_hashes=skipped_state,
            replacement_requests=replacement_requests,
        ),
    )
    if final_state_saved:
        volatile_requests.clear()
    elif volatile_requests:
        raise StateError(
            "Replacement checkpoints could not be persisted; preserving them in "
            "memory and aborting this cleanup run"
        )
    LOGGER.info("Cleanup run completed successfully")


def _prune_replacement_requests(
    replacement_requests: dict[str, str],
    remaining_torrent_copies: Counter[str],
    all_instances_processed: bool,
) -> None:
    """Drop checkpoints only after every configured instance confirms no copy remains."""
    if not all_instances_processed:
        return
    for torrent_hash in list(replacement_requests):
        if remaining_torrent_copies[torrent_hash] <= 0:
            replacement_requests.pop(torrent_hash, None)


def _request_replacements(
    matches: list[Match],
    radarr_configs: list[RadarrConfig],
    coordinators: dict[str, ReplacementCoordinator],
    qbit_client: object,
    require_tracker_contains: str,
    dry_run: bool,
    max_deletes_per_run: int,
    replacement_requests: dict[str, str],
    skipped: list[tuple[Match, str]],
    unpersisted_checkpoints: set[str] | None = None,
    checkpoint_state_path: str | Path | None = None,
    volatile_replacement_requests: dict[str, str] | None = None,
) -> list[Match]:
    removable: list[Match] = []
    live_processed = 0
    for match in matches:
        tracker_ok = tracker_verified(
            qbit_client, match.torrent.hash, require_tracker_contains
        )
        if not tracker_ok:
            # remove_matches will record the normal tracker-verification skip.
            removable.append(match)
            continue
        if not dry_run and live_processed >= max_deletes_per_run:
            reason = f"max_deletes_per_run cap reached ({max_deletes_per_run})"
            LOGGER.warning(
                "Skipping replacement and removal for %s on %s: %s",
                match.torrent.hash,
                match.instance_name,
                reason,
            )
            skipped.append((match, reason))
            continue
        replacement_id = match.ptp.replacement_torrent_id
        if replacement_id is None:
            removable.append(match)
            if not dry_run:
                live_processed += 1
            continue
        route = radarr_route(
            radarr_configs, match.instance_name, match.torrent.category
        )
        if route is None:
            LOGGER.info(
                "No Radarr route for qBittorrent instance=%s category=%r; "
                "continuing cleanup without automatic replacement",
                match.instance_name,
                match.torrent.category,
            )
            removable.append(match)
            if not dry_run:
                live_processed += 1
            continue
        coordinator = coordinators.get(route.name.casefold())
        if coordinator is None:
            skipped.append((match, f"Radarr route {route.name!r} is unavailable"))
            continue
        if dry_run:
            LOGGER.info(
                "DRY RUN: would request PTP replacement torrent %s through Radarr %s "
                "before cleanup",
                replacement_id,
                route.name,
            )
            removable.append(match)
            continue
        checkpoint = _checkpoint_key(route, match.torrent.hash)
        if checkpoint in (unpersisted_checkpoints or set()):
            reason = (
                "replacement checkpoint is not durable; preserved to prevent "
                "duplicate download"
            )
            LOGGER.error("Preserving %s: %s", checkpoint, reason)
            skipped.append((match, reason))
            continue
        if replacement_requests.get(checkpoint) == replacement_id:
            LOGGER.info(
                "Replacement %s was already requested for %s; continuing cleanup",
                replacement_id,
                checkpoint,
            )
            removable.append(match)
            live_processed += 1
            continue
        try:
            coordinator.replace(match)
            replacement_requests[checkpoint] = replacement_id
            if checkpoint_state_path is not None:
                checkpoint_saved = save_state(
                    checkpoint_state_path,
                    State(replacement_requests=replacement_requests),
                )
                if checkpoint_saved:
                    # The complete map was persisted, including any checkpoint that
                    # failed earlier in this run.
                    if unpersisted_checkpoints is not None:
                        unpersisted_checkpoints.clear()
                    if volatile_replacement_requests is not None:
                        volatile_replacement_requests.clear()
                else:
                    if unpersisted_checkpoints is not None:
                        unpersisted_checkpoints.add(checkpoint)
                    if volatile_replacement_requests is not None:
                        volatile_replacement_requests[checkpoint] = replacement_id
                    reason = (
                        "replacement requested but checkpoint could not be saved; "
                        "preserved to prevent unsafe cleanup"
                    )
                    LOGGER.error("Preserving %s: %s", checkpoint, reason)
                    skipped.append((match, reason))
                    continue
        except Exception as exc:
            LOGGER.exception(
                "Unable to request replacement %s for %s", replacement_id, match.torrent.hash
            )
            if route.preserve_on_failure:
                skipped.append((match, f"replacement failed; preserved for retry: {exc}"))
                continue
        removable.append(match)
        live_processed += 1
    return removable


def run_daemon(config: Config | None = None) -> None:
    cfg = config or load_config_from_env()
    interval_seconds = cfg.app.interval_days * 24 * 60 * 60
    if os.environ.get("RUN_ONCE", "").lower() in {"1", "true", "yes", "on"}:
        LOGGER.info("RUN_ONCE override is set; running once and exiting")
        run_once(cfg)
        return

    LOGGER.info("Starting daemon with interval_days=%s", cfg.app.interval_days)
    volatile_replacement_requests: dict[str, str] = {}
    with ExitStack() as stack:
        ptp_client = None
        coordinators = None
        if cfg.radarr:
            ptp_client = PtpClient(cfg.ptp, cfg.credentials)
            coordinators = _start_replacement_services(stack, cfg, ptp_client)
        if cfg.app.run_on_startup:
            _run_daemon_cleanup(
                cfg, ptp_client, coordinators, volatile_replacement_requests
            )
        while True:
            LOGGER.info("Sleeping %.0f seconds until next cleanup run", interval_seconds)
            time.sleep(interval_seconds)
            _run_daemon_cleanup(
                cfg, ptp_client, coordinators, volatile_replacement_requests
            )


def _run_daemon_cleanup(
    config: Config,
    ptp_client: PtpClient | None = None,
    coordinators: dict[str, ReplacementCoordinator] | None = None,
    volatile_replacement_requests: dict[str, str] | None = None,
) -> None:
    try:
        if ptp_client is None and coordinators is None:
            run_once(
                config,
                volatile_replacement_requests=volatile_replacement_requests,
            )
        else:
            run_once(
                config,
                ptp_client=ptp_client,
                coordinators=coordinators,
                volatile_replacement_requests=volatile_replacement_requests,
            )
    except ConfigError:
        raise
    except Exception:
        LOGGER.exception("Cleanup run failed; daemon will retry after the configured interval")


def _start_replacement_services(
    stack: ExitStack, config: Config, ptp_client: PtpClient
) -> dict[str, ReplacementCoordinator]:
    coordinators: dict[str, ReplacementCoordinator] = {}
    for radarr in config.radarr:
        catalog = ReplacementCatalog()
        server = ReplacementServer(
            radarr.torznab_host,
            radarr.torznab_port,
            radarr.torznab_external_url,
            radarr.torznab_api_key,
            catalog,
            ptp_client,
        )
        stack.enter_context(server)
        coordinators[radarr.name.casefold()] = ReplacementCoordinator(
            ptp_client, RadarrClient(radarr), catalog
        )
        LOGGER.info(
            "Replacement Torznab server for Radarr %s listening on port %s",
            radarr.name,
            radarr.torznab_port,
        )
    return coordinators


def _checkpoint_key(radarr: RadarrConfig, torrent_hash: str) -> str:
    return "|".join((radarr.name.casefold(), torrent_hash.strip().lower()))
