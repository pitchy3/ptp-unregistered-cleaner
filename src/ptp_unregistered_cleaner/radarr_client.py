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
        movies = self._request("GET", "/movie")
        if not isinstance(movies, list):
            raise RadarrClientError("Radarr movie response was not a list")
        matches = [
            movie
            for movie in movies
            if isinstance(movie, dict)
            and (
                (imdb_id and str(movie.get("imdbId", "")) == imdb_id)
                or (tmdb_id and str(movie.get("tmdbId", "")) == tmdb_id)
            )
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
