import httpx
import pytest

from ptp_unregistered_cleaner.config import QBittorrentConfig
from ptp_unregistered_cleaner.qbittorrent_client import (
    QBittorrentClient,
    QBittorrentClientError,
)


def test_http_status_error_is_wrapped_as_client_error() -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    client = QBittorrentClient.__new__(QBittorrentClient)
    client.config = QBittorrentConfig("main", "http://main", "user", "pass")
    client.client = httpx.Client(
        base_url="http://main",
        transport=httpx.MockTransport(unavailable),
        trust_env=False,
    )
    client._authenticated = True

    try:
        with pytest.raises(QBittorrentClientError, match="main: HTTP 503"):
            client.list_torrents()
    finally:
        client.close()
