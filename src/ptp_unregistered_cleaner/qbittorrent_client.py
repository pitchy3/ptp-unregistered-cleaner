"""Minimal qBittorrent Web API client."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .config import QBittorrentConfig

LOGGER = logging.getLogger(__name__)


class QBittorrentClientError(RuntimeError):
    """Raised when qBittorrent cannot be queried or updated."""


@dataclass(frozen=True)
class Torrent:
    hash: str
    name: str
    category: str = ""
    tags: list[str] | None = None


@dataclass(frozen=True)
class Tracker:
    url: str


class QBittorrentClient:
    def __init__(self, config: QBittorrentConfig) -> None:
        import httpx

        self.config = config
        self.client = httpx.Client(base_url=config.url, timeout=30, follow_redirects=True)
        self._authenticated = False

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> QBittorrentClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def login(self) -> None:
        try:
            response = self.client.post(
                "/api/v2/auth/login",
                data={"username": self.config.username, "password": self.config.password},
            )
        except Exception as exc:
            import httpx

            if not isinstance(exc, httpx.HTTPError):
                raise
            raise QBittorrentClientError(
                f"qBittorrent auth request failed for instance {self.config.name}: {exc}"
            ) from exc
        if response.status_code != 200 or response.text.strip() != "Ok.":
            raise QBittorrentClientError(
                f"qBittorrent auth failure for instance {self.config.name}"
            )
        self._authenticated = True

    def list_torrents(self) -> list[Torrent]:
        self._ensure_login()
        response = self._request("GET", "/api/v2/torrents/info")
        data = response.json()
        if not isinstance(data, list):
            raise QBittorrentClientError(
                f"qBittorrent torrent list for instance {self.config.name} was not an array"
            )
        torrents = []
        for item in data:
            if not isinstance(item, dict):
                continue
            torrent_hash = str(item.get("hash", "")).strip().lower()
            if not torrent_hash:
                continue
            torrents.append(
                Torrent(
                    hash=torrent_hash,
                    name=str(item.get("name", "")),
                    category=str(item.get("category", "") or ""),
                    tags=_parse_tags(item.get("tags")),
                )
            )
        return torrents

    def get_trackers(self, torrent_hash: str) -> list[Tracker]:
        self._ensure_login()
        response = self._request("GET", "/api/v2/torrents/trackers", params={"hash": torrent_hash})
        data = response.json()
        if not isinstance(data, list):
            return []
        return [Tracker(url=str(item.get("url", ""))) for item in data if isinstance(item, dict)]

    def delete_torrent(self, torrent_hash: str) -> None:
        self._ensure_login()
        # Safety invariant: remove the torrent entry only. Downloaded files are never deleted.
        response = self._request(
            "POST",
            "/api/v2/torrents/delete",
            data={"hashes": torrent_hash, "deleteFiles": "false"},
        )
        if response.status_code not in (200, 204):
            raise QBittorrentClientError(
                f"qBittorrent delete failed for instance {self.config.name}, hash {torrent_hash}: "
                f"HTTP {response.status_code}"
            )

    def _ensure_login(self) -> None:
        if not self._authenticated:
            self.login()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self.client.request(method, path, **kwargs)
        except Exception as exc:
            import httpx

            if not isinstance(exc, httpx.HTTPError):
                raise
            raise QBittorrentClientError(
                f"qBittorrent request failed for instance {self.config.name}: {exc}"
            ) from exc
        if response.status_code in (401, 403):
            raise QBittorrentClientError(
                f"qBittorrent auth failure for instance {self.config.name}"
            )
        response.raise_for_status()
        return response


def _parse_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]
