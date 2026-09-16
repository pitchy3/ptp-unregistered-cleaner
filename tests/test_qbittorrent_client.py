import httpx
import pytest

from ptp_unregistered_cleaner.config import QBittorrentConfig
from ptp_unregistered_cleaner.qbittorrent_client import (
    QBittorrentClient,
    QBittorrentClientError,
)


def _client_with_response(status_code: int, content: bytes = b"") -> QBittorrentClient:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=content, request=request)

    client = QBittorrentClient.__new__(QBittorrentClient)
    client.config = QBittorrentConfig("main", "http://main", "user", "pass")
    client.client = httpx.Client(
        base_url="http://main",
        transport=httpx.MockTransport(respond),
        trust_env=False,
    )
    client._authenticated = True
    return client


def test_http_status_error_is_wrapped_as_client_error() -> None:
    client = _client_with_response(503)

    try:
        with pytest.raises(QBittorrentClientError, match="main: HTTP 503"):
            client.list_torrents()
    finally:
        client.close()


@pytest.mark.parametrize(
    ("operation", "expected_message"),
    [
        (lambda client: client.list_torrents(), "torrent list.*main.*not valid JSON"),
        (lambda client: client.get_trackers("abc"), "tracker list.*main.*not valid JSON"),
    ],
)
def test_malformed_json_is_wrapped_as_client_error(operation, expected_message: str) -> None:
    client = _client_with_response(200, b"not json")

    try:
        with pytest.raises(QBittorrentClientError, match=expected_message):
            operation(client)
    finally:
        client.close()
