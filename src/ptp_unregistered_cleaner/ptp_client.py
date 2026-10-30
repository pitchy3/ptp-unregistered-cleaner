"""PassThePopcorn unregistered-history API client."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .config import Credentials, PtpConfig

LOGGER = logging.getLogger(__name__)

_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
_REASON_URL_RE = re.compile(r"https?://[^\s<>\"\x27]+", re.IGNORECASE)
_SENSITIVE_QUERY_PARAMS = {
    "apiuser",
    "apikey",
    "passkey",
    "token",
    "password",
    "sid",
    "auth",
}


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

    @property
    def replacement_torrent_id(self) -> str | None:
        """Return the explicitly linked successor from PTP's ReasonText, if valid."""
        if not self.group_id:
            return None

        urls = _REASON_URL_RE.findall(self.reason_text or "")
        # Multiple links are ambiguous; never guess which torrent to request.
        if len(urls) != 1:
            return None
        try:
            link = urlsplit(urls[0].rstrip(".,;)]}"))
        except ValueError:
            return None
        if (
            link.scheme.lower() != "https"
            or link.hostname != "passthepopcorn.me"
            or link.username is not None
            or link.password is not None
            or link.path != "/torrents.php"
        ):
            return None

        params = parse_qs(link.query)
        group_ids = params.get("id", [])
        torrent_ids = params.get("torrentid", [])
        if len(group_ids) != 1 or len(torrent_ids) != 1:
            return None
        group_id, torrent_id = group_ids[0], torrent_ids[0]
        if (
            not group_id.isdigit()
            or group_id != self.group_id
            or not torrent_id.isdigit()
            or torrent_id == self.torrent_id
        ):
            return None
        return torrent_id

    @property
    def has_invalid_replacement_reference(self) -> bool:
        """Preserve trumped torrents if their successor link is missing or unsafe."""
        reason_text = self.reason_text or ""
        return self.replacement_torrent_id is None and (
            (self.reason or "").strip() == "3"
            or bool(_REASON_URL_RE.search(reason_text))
            or "torrents.php" in reason_text.lower()
            or "torrentid=" in reason_text.lower()
        )


@dataclass(frozen=True)
class ReplacementTorrent:
    torrent_id: str
    group_id: str
    title: str
    size: int
    imdb_id: str | None = None
    tmdb_id: str | None = None
    seeders: int = 0
    peers: int = 0


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

    def fetch_replacement(self, group_id: str, torrent_id: str) -> ReplacementTorrent:
        """Fetch and validate a PTP-designated replacement torrent's metadata."""
        import httpx

        url = urljoin(f"{self.config.base_url}/", "torrents.php")
        headers = {
            "ApiUser": self.credentials.ptp_api_user,
            "ApiKey": self.credentials.ptp_api_key,
        }
        try:
            response = httpx.get(
                url,
                params={"id": group_id, "torrentid": torrent_id, "json": "1"},
                headers=headers,
                timeout=self.config.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise PtpClientError(
                f"Unable to fetch replacement torrent {torrent_id}: {exc}"
            ) from exc
        if response.status_code != 200:
            raise PtpClientError(
                f"PTP returned HTTP {response.status_code} for replacement torrent {torrent_id}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PtpClientError("PTP replacement metadata was not valid JSON") from exc
        return extract_replacement_torrent(payload, group_id, torrent_id)

    def download_torrent(self, torrent_id: str) -> bytes:
        """Download one authenticated .torrent while keeping PTP credentials server-side."""
        import httpx

        url = urljoin(f"{self.config.base_url}/", "torrents.php")
        headers = {
            "ApiUser": self.credentials.ptp_api_user,
            "ApiKey": self.credentials.ptp_api_key,
        }
        try:
            response = httpx.get(
                url,
                params={"action": "download", "id": torrent_id},
                headers=headers,
                timeout=self.config.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise PtpClientError(f"Unable to download PTP torrent {torrent_id}: {exc}") from exc
        if response.status_code != 200:
            raise PtpClientError(
                f"PTP returned HTTP {response.status_code} downloading torrent {torrent_id}"
            )
        if not response.content.startswith(b"d"):
            raise PtpClientError(
                f"PTP response for torrent {torrent_id} was not a bencoded torrent"
            )
        return response.content

    def _fetch_page(self, client: Any, page: int) -> dict[str, Any]:
        params = {
            "action": "unregistered",
            "type": "json",
            self.config.page_parameter: page,
        }
        # PTP requires API credentials in request headers, not query parameters.
        # Do not log these headers.
        headers = {
            "ApiUser": self.credentials.ptp_api_user,
            "ApiKey": self.credentials.ptp_api_key,
        }
        try:
            response = client.get(self.url, params=params, headers=headers)
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

        if response.status_code in _REDIRECT_STATUS_CODES:
            location = response.headers.get("Location")
            location_hint = (
                f" Redirect Location: {_sanitize_url_query(location)}." if location else ""
            )
            raise PtpClientError(
                f"PTP redirected the API request with HTTP {response.status_code} for page {page}. "
                "This usually means credentials were rejected, API access is disabled, or "
                "the request was treated like normal website traffic. Confirm PTP_API_USER, "
                f"PTP_API_KEY, and API privileges.{location_hint}"
            )

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


def _sanitize_url_query(url: str) -> str:
    parts = urlsplit(url)
    sanitized_query = urlencode(
        [
            (key, "***" if key.lower() in _SENSITIVE_QUERY_PARAMS else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ],
        safe="*",
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, sanitized_query, parts.fragment))


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def extract_replacement_torrent(
    payload: dict[str, Any], expected_group_id: str, expected_torrent_id: str
) -> ReplacementTorrent:
    """Find the exact successor within PTP's movie-group JSON response."""
    if not isinstance(payload, dict):
        raise PtpClientError("PTP replacement metadata response was not an object")
    actual_group = _optional_str(payload.get("GroupId"))
    if actual_group != expected_group_id:
        raise PtpClientError(
            f"PTP returned group {actual_group!r}, expected {expected_group_id!r}"
        )

    torrents = payload.get("Torrents")
    if not isinstance(torrents, list):
        raise PtpClientError("PTP replacement metadata did not include a torrent list")
    matches = [
        torrent
        for torrent in torrents
        if isinstance(torrent, dict)
        and _optional_str(torrent.get("Id")) == expected_torrent_id
    ]
    if len(matches) != 1:
        raise PtpClientError(
            f"Expected exactly one torrent {expected_torrent_id} in group "
            f"{expected_group_id}; found {len(matches)}"
        )
    torrent = matches[0]
    title = _optional_str(torrent.get("ReleaseName"))
    if not title:
        raise PtpClientError("PTP replacement metadata did not include a release name")
    size = _int_or_default(torrent.get("Size"), 0)
    if size <= 0:
        raise PtpClientError("PTP replacement metadata did not include a positive size")
    seeders = _int_or_default(torrent.get("Seeders"), 0)
    leechers = _int_or_default(torrent.get("Leechers"), 0)
    if seeders <= 0:
        raise PtpClientError("PTP replacement torrent has no seeders")

    imdb = _optional_str(payload.get("ImdbId"))
    tmdb = _optional_str(payload.get("TmdbId"))
    if imdb and imdb.isdigit():
        imdb = f"tt{imdb}"
    return ReplacementTorrent(
        expected_torrent_id,
        actual_group,
        title,
        size,
        imdb,
        tmdb,
        seeders,
        seeders + leechers,
    )
