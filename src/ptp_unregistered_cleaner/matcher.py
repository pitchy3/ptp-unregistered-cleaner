"""Matching and cleanup orchestration helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from .config import MatchingConfig
from .ptp_client import UnregisteredTorrent
from .qbittorrent_client import Torrent

LOGGER = logging.getLogger(__name__)


class CleanerQBittorrentClient(Protocol):
    def get_trackers(self, torrent_hash: str): ...

    def delete_torrent(self, torrent_hash: str) -> None: ...


@dataclass(frozen=True)
class Match:
    instance_name: str
    torrent: Torrent
    ptp: UnregisteredTorrent


def torrent_allowed(torrent: Torrent, config: MatchingConfig) -> tuple[bool, str | None]:
    category = (torrent.category or "").lower()
    tags = {tag.lower() for tag in (torrent.tags or [])}
    include_categories = {item.lower() for item in config.include_categories}
    exclude_categories = {item.lower() for item in config.exclude_categories}
    include_tags = {item.lower() for item in config.include_tags}
    exclude_tags = {item.lower() for item in config.exclude_tags}

    if category in exclude_categories:
        return False, f"category '{torrent.category}' is excluded"
    if include_categories and category not in include_categories:
        return False, f"category '{torrent.category}' is not included"
    if tags & exclude_tags:
        return False, "one or more tags are excluded"
    if include_tags and not (tags & include_tags):
        return False, "no required include tag is present"
    return True, None


def find_matches(
    instance_name: str,
    torrents: list[Torrent],
    ptp_torrents: dict[str, UnregisteredTorrent],
    matching_config: MatchingConfig,
) -> tuple[list[Match], list[tuple[Torrent, str]]]:
    matches: list[Match] = []
    skipped: list[tuple[Torrent, str]] = []
    for torrent in torrents:
        normalized_hash = torrent.hash.strip().lower()
        if normalized_hash not in ptp_torrents:
            continue
        allowed, reason = torrent_allowed(torrent, matching_config)
        if not allowed:
            skipped.append((torrent, reason or "filtered by matching config"))
            continue
        matches.append(Match(instance_name, torrent, ptp_torrents[normalized_hash]))
    return matches, skipped


def tracker_verified(client: CleanerQBittorrentClient, torrent_hash: str, required: str) -> bool:
    if not required:
        return True
    needle = required.lower()
    trackers = client.get_trackers(torrent_hash)
    return any(needle in tracker.url.lower() for tracker in trackers)


def remove_matches(
    client: CleanerQBittorrentClient,
    matches: list[Match],
    *,
    dry_run: bool,
    max_deletes_per_run: int,
    require_tracker_contains: str,
) -> tuple[list[Match], list[tuple[Match, str]]]:
    """Remove matched torrents, respecting dry-run, tracker checks, and the safety cap."""
    removed: list[Match] = []
    skipped: list[tuple[Match, str]] = []
    cap = max_deletes_per_run
    if dry_run and len(matches) > cap:
        LOGGER.warning(
            "DRY RUN: %s matches exceed max_deletes_per_run=%s; "
            "live mode would only remove up to cap",
            len(matches),
            cap,
        )

    live_processed = 0
    for match in matches:
        _log_candidate(match)
        if not tracker_verified(client, match.torrent.hash, require_tracker_contains):
            reason = (
                "tracker verification failed; "
                f"no tracker contains '{require_tracker_contains}'"
            )
            LOGGER.info("Skipping %s on %s: %s", match.torrent.hash, match.instance_name, reason)
            skipped.append((match, reason))
            continue
        if dry_run:
            LOGGER.info(
                "DRY RUN: would remove torrent from %s: name=%r hash=%s",
                match.instance_name,
                match.torrent.name,
                match.torrent.hash,
            )
            continue
        if live_processed >= cap:
            reason = f"max_deletes_per_run cap reached ({cap})"
            LOGGER.warning("Skipping %s on %s: %s", match.torrent.hash, match.instance_name, reason)
            skipped.append((match, reason))
            continue
        client.delete_torrent(match.torrent.hash)
        live_processed += 1
        removed.append(match)
        LOGGER.info(
            "Removed torrent from %s without deleting files: name=%r hash=%s",
            match.instance_name,
            match.torrent.name,
            match.torrent.hash,
        )
    return removed, skipped


def _log_candidate(match: Match) -> None:
    LOGGER.info(
        "Candidate match: instance=%s torrent_name=%r torrent_hash=%s ptp_file_name=%r "
        "ptp_torrent_id=%r ptp_reason_text=%r ptp_deleted_time=%r",
        match.instance_name,
        match.torrent.name,
        match.torrent.hash,
        match.ptp.file_name,
        match.ptp.torrent_id,
        match.ptp.reason_text,
        match.ptp.deleted_time,
    )
