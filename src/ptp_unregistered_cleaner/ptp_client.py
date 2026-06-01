"""PassThePopcorn unregistered-history API client."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from .config import Credentials, PtpConfig

LOGGER = logging.getLogger(__name__)


class PtpClientError(RuntimeError):
    """Raised when the PTP API cannot be queried successfully."""


@dataclass(frozen=True)
class UnregisteredTorrent:
    infohash: str
    torrent_id: str | None = None
    group_id: str | None = None
    file_name: str | None = None
    file_size: str | int | None = None
    reason: str | None = None
    reason_text: str | None = None
    deleted_time: str | None = None
    announce_time: str | None = None
    ip: str | None = None
    user_agent: str | None = None


def normalize_infohash(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def extract_unregistered_torrents(payload: dict[str, Any]) -> dict[str, UnregisteredTorrent]:
    """Extract normalized, deduplicated InfoHash entries from a PTP response payload."""
    entries = payload.get("Unregistered", [])
    if not isinstance(entries, list):
        raise PtpClientError("PTP response field 'Unregistered' was not an array")

    torrents: dict[str, UnregisteredTorrent] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        infohash = normalize_infohash(item.get("InfoHash"))
        if not infohash:
            continue
        torrents.setdefault(
            infohash,
            UnregisteredTorrent(
                infohash=infohash,
                torrent_id=_optional_str(item.get("TorrentID")),
                group_id=_optional_str(item.get("GroupID")),
                file_name=_optional_str(item.get("FileName")),
                file_size=item.get("FileSize"),
                reason=_optional_str(item.get("Reason")),
                reason_text=_optional_str(item.get("ReasonText")),
                deleted_time=_optional_str(item.get("DeletedTime")),
                announce_time=_optional_str(item.get("AnnounceTime")),
                ip=_optional_str(item.get("IP")),
                user_agent=_optional_str(item.get("UserAgent")),
            ),
        )
    return torrents


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


class PtpClient:
    def __init__(self, config: PtpConfig, credentials: Credentials) -> None:
        self.config = config
        self.credentials = credentials
        self.url = urljoin(f"{config.base_url}/", config.unregistered_path.lstrip("/"))

    def fetch_unregistered(self) -> dict[str, UnregisteredTorrent]:
        page = 1
        pages = 1
        all_torrents: dict[str, UnregisteredTorrent] = {}
        total_returned = 0

        import httpx

        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            while page <= pages:
                if page > 1 and self.config.min_interval_seconds_between_pages > 0:
                    time.sleep(self.config.min_interval_seconds_between_pages)
                payload = self._fetch_page(client, page)
                page_torrents = extract_unregistered_torrents(payload)
                total_returned += len(payload.get("Unregistered", []) or [])
                all_torrents.update(page_torrents)

                current_page = _int_or_default(payload.get("Page"), page)
                pages = _int_or_default(payload.get("Pages"), pages)
                if pages < current_page:
                    break
                page = current_page + 1

        LOGGER.info(
            "PTP returned %s unregistered torrent rows and %s unique infohashes",
            total_returned,
            len(all_torrents),
        )
        return all_torrents

    def _fetch_page(self, client: Any, page: int) -> dict[str, Any]:
        params = {
            "action": "unregistered",
            "type": "json",
            self.config.page_parameter: page,
        }
        # PTP API credentials are attached in one central place. Do not log these params.
        params.update(
            {"ApiUser": self.credentials.ptp_api_user, "ApiKey": self.credentials.ptp_api_key}
        )
        try:
            response = client.get(self.url, params=params)
        except Exception as exc:
            import httpx

            if isinstance(exc, httpx.TimeoutException):
                raise PtpClientError(
                    f"Timed out querying PTP unregistered API page {page}"
                ) from exc
            if isinstance(exc, httpx.HTTPError):
                raise PtpClientError(
                    f"HTTP error querying PTP unregistered API page {page}: {exc}"
                ) from exc
            raise

        if response.status_code != 200:
            hint = ""
            if response.status_code == 400:
                hint = " 400 may indicate malformed API credentials."
            elif response.status_code == 401:
                hint = " 401 may indicate API privileges are disabled."
            raise PtpClientError(
                f"PTP API returned HTTP {response.status_code} for page {page}.{hint}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PtpClientError(f"PTP API returned invalid JSON for page {page}") from exc
        if not isinstance(payload, dict):
            raise PtpClientError("PTP API JSON response was not an object")
        return payload


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
