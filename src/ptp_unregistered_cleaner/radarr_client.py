"""Minimal Radarr API client for verified, explicit release grabs."""

from __future__ import annotations

from typing import Any

import httpx

from .config import RadarrConfig


class RadarrClientError(RuntimeError):
    pass


class RadarrClient:
    def __init__(self, config: RadarrConfig) -> None:
        self.config = config
        self.headers = {"X-Api-Key": config.api_key}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = httpx.request(
                method,
                f"{self.config.url}/api/v3{path}",
                headers=self.headers,
                timeout=self.config.timeout_seconds,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise RadarrClientError(f"Radarr request failed: {exc}") from exc
        if response.status_code >= 400:
            raise RadarrClientError(
                f"Radarr {method} {path} returned HTTP {response.status_code}"
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise RadarrClientError("Radarr returned invalid JSON") from exc

    def find_movie(self, imdb_id: str | None, tmdb_id: str | None) -> dict[str, Any]:
        if not imdb_id and not tmdb_id:
            raise RadarrClientError("At least one movie identifier is required")
        movies = self._request("GET", "/movie")
        if not isinstance(movies, list):
            raise RadarrClientError("Radarr movie response was not a list")
        matches = [
            movie
            for movie in movies
            if isinstance(movie, dict)
            and (not imdb_id or str(movie.get("imdbId", "")) == imdb_id)
            and (not tmdb_id or str(movie.get("tmdbId", "")) == tmdb_id)
        ]
        if len(matches) != 1:
            raise RadarrClientError(
                f"Expected exactly one Radarr movie for IMDb={imdb_id!r} TMDb={tmdb_id!r}; "
                f"found {len(matches)}"
            )
        return matches[0]

    def find_release(self, movie_id: int, guid: str) -> dict[str, Any]:
        releases = self._request("GET", "/release", params={"movieId": movie_id})
        matches = [
            release
            for release in releases
            if isinstance(release, dict) and release.get("guid") == guid
        ]
        if len(matches) != 1:
            raise RadarrClientError(f"Expected one cached release {guid!r}; found {len(matches)}")
        return matches[0]

    def grab(self, release: dict[str, Any], movie_id: int) -> None:
        if int(release.get("mappedMovieId") or 0) != movie_id:
            raise RadarrClientError("Replacement release mapped to a different Radarr movie")
        if not release.get("downloadAllowed"):
            raise RadarrClientError("Radarr marked replacement download as not allowed")
        rejections = release.get("rejections") or []
        if not isinstance(rejections, list):
            raise RadarrClientError("Radarr returned malformed release rejections")
        if not release.get("approved") and not rejections:
            raise RadarrClientError(
                "Radarr did not approve the replacement and provided no safe policy rejection"
            )
        unsafe = [reason for reason in rejections if not _safe_policy_rejection(str(reason))]
        if unsafe:
            raise RadarrClientError(
                "Refusing to bypass Radarr rejection(s): " + "; ".join(map(str, unsafe))
            )
        self._request(
            "POST",
            "/release",
            json={"guid": release["guid"], "indexerId": release["indexerId"]},
        )

    def replacement_was_grabbed(
        self, movie_id: int, guid: str, title: str
    ) -> bool:
        """Reconcile an uncertain grab against Radarr's queue and history."""
        queue = self._request(
            "GET",
            "/queue",
            params={"movieIds": movie_id, "page": 1, "pageSize": 100},
        )
        for record in _records(queue, "queue"):
            if _same_movie(record, movie_id) and _same_release(record, guid, title):
                return True

        history = self._request(
            "GET",
            "/history",
            params={
                "movieId": movie_id,
                "page": 1,
                "pageSize": 100,
                "sortKey": "date",
                "sortDirection": "descending",
            },
        )
        return any(
            _same_movie(record, movie_id)
            and str(record.get("eventType", "")).casefold() == "grabbed"
            and _same_release(record, guid, title)
            for record in _records(history, "history")
        )


def _safe_policy_rejection(reason: str) -> bool:
    normalized = reason.casefold()
    return any(
        phrase in normalized
        for phrase in (
            "equal or higher preference",
            "not an upgrade",
            "repack downloading is disabled",
            "repack for a different release group",
        )
    )


def _records(payload: Any, response_name: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise RadarrClientError(f"Radarr {response_name} response was malformed")
    return [record for record in payload["records"] if isinstance(record, dict)]


def _same_movie(record: dict[str, Any], movie_id: int) -> bool:
    return int(record.get("movieId") or 0) == movie_id


def _same_release(record: dict[str, Any], guid: str, title: str) -> bool:
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    record_guid = str(data.get("guid") or record.get("guid") or "")
    if record_guid:
        return record_guid == guid
    record_title = str(record.get("title") or record.get("sourceTitle") or "")
    return record_title == title
