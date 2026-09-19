"""PTP trump-to-Radarr replacement orchestration."""

from __future__ import annotations

import logging

from .matcher import Match
from .ptp_client import PtpClient
from .radarr_client import RadarrClient
from .torznab import ReplacementCatalog

LOGGER = logging.getLogger(__name__)


class ReplacementError(RuntimeError):
    pass


class ReplacementCoordinator:
    def __init__(
        self, ptp: PtpClient, radarr: RadarrClient, catalog: ReplacementCatalog
    ) -> None:
        self.ptp = ptp
        self.radarr = radarr
        self.catalog = catalog

    def replace(self, match: Match) -> str | None:
        replacement_id = match.ptp.replacement_torrent_id
        if replacement_id is None:
            return None
        if not match.ptp.group_id:
            raise ReplacementError("PTP trump row has no group ID")
        replacement = self.ptp.fetch_replacement(match.ptp.group_id, replacement_id)
        if not replacement.imdb_id and not replacement.tmdb_id:
            raise ReplacementError("PTP replacement has no IMDb or TMDb ID for safe mapping")
        movie = self.radarr.find_movie(replacement.imdb_id, replacement.tmdb_id)
        if not movie.get("hasFile") or not int(movie.get("movieFileId") or 0):
            raise ReplacementError(
                "Mapped Radarr movie has no existing file to replace"
            )
        movie_id = int(movie["id"])
        entry = self.catalog.publish(replacement)
        try:
            release = self.radarr.find_release(movie_id, entry.guid)
            self.radarr.grab(release, movie_id)
        finally:
            # The catalog exists only to let Radarr discover and fetch this exact
            # candidate during the explicit grab. Never leave a rejected or already
            # consumed release available to later RSS/search requests.
            self.catalog.discard(entry.guid)
        LOGGER.info(
            "Requested verified PTP replacement via Radarr: old_hash=%s old_torrent_id=%s "
            "replacement_torrent_id=%s movie_id=%s",
            match.torrent.hash,
            match.ptp.torrent_id,
            replacement_id,
            movie_id,
        )
        return replacement_id
