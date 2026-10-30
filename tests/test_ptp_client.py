import pytest

from ptp_unregistered_cleaner.config import Credentials, PtpConfig
from ptp_unregistered_cleaner.ptp_client import (
    PtpClient,
    PtpClientError,
    extract_unregistered_torrents,
)


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self) -> dict:
        return self._payload


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests: list[dict] = []

    def get(self, url: str, **kwargs: dict) -> FakeResponse:
        self.requests.append({"url": url, **kwargs})
        return self.response


def test_infohash_extraction_normalizes_and_dedupes() -> None:
    payload = {
        "Unregistered": [
            {"InfoHash": " ABCDEF ", "TorrentID": 1, "FileName": "first.mkv"},
            {"InfoHash": "abcdef", "TorrentID": 2, "FileName": "duplicate.mkv"},
            {"InfoHash": "123456"},
            {"InfoHash": ""},
            {},
        ]
    }
    torrents = extract_unregistered_torrents(payload)
    assert sorted(torrents) == ["123456", "abcdef"]
    assert torrents["abcdef"].torrent_id == "1"
    assert torrents["abcdef"].file_name == "first.mkv"


def test_unregistered_must_be_array() -> None:
    with pytest.raises(PtpClientError):
        extract_unregistered_torrents({"Unregistered": {}})


def test_fetch_page_sends_credentials_as_headers_not_params() -> None:
    config = PtpConfig(page_parameter="page")
    credentials = Credentials(ptp_api_user="user-id", ptp_api_key="secret-key")
    ptp_client = PtpClient(config, credentials)
    fake_client = FakeClient(
        FakeResponse(payload={"Unregistered": [], "Page": 1, "Pages": 1})
    )

    payload = ptp_client._fetch_page(fake_client, 1)

    assert payload == {"Unregistered": [], "Page": 1, "Pages": 1}
    assert len(fake_client.requests) == 1
    request = fake_client.requests[0]
    assert request["url"] == "https://passthepopcorn.me/userhistory.php"
    assert request["params"] == {
        "action": "unregistered",
        "type": "json",
        "page": 1,
    }
    assert "ApiUser" not in request["params"]
    assert "ApiKey" not in request["params"]
    assert request["headers"] == {"ApiUser": "user-id", "ApiKey": "secret-key"}


def test_fetch_page_redirect_error_masks_sensitive_location_params() -> None:
    config = PtpConfig()
    credentials = Credentials(ptp_api_user="user-id", ptp_api_key="secret-key")
    ptp_client = PtpClient(config, credentials)
    fake_client = FakeClient(
        FakeResponse(
            status_code=302,
            headers={
                "Location": (
                    "https://passthepopcorn.me/login.php?ApiUser=user-id&ApiKey=secret-key"
                    "&passkey=pass&token=tok&password=pw&sid=session&auth=auth-value"
                    "&safe=value"
                )
            },
        )
    )

    with pytest.raises(PtpClientError) as exc_info:
        ptp_client._fetch_page(fake_client, 1)

    message = str(exc_info.value)
    assert "redirected the API request" in message
    assert "credentials were rejected" in message
    assert "API access is disabled" in message
    assert "API privileges" in message
    assert "ApiUser=***" in message
    assert "ApiKey=***" in message
    assert "passkey=***" in message
    assert "token=***" in message
    assert "password=***" in message
    assert "sid=***" in message
    assert "auth=***" in message
    assert "safe=value" in message
    assert "user-id" not in message
    assert "secret-key" not in message
    assert "passkey=pass" not in message
    assert "token=tok" not in message
    assert "pw" not in message
    assert "session" not in message
    assert "auth-value" not in message


def test_fetch_replacement_uses_movie_json_with_api_headers(monkeypatch) -> None:
    import httpx

    client = PtpClient(
        PtpConfig(),
        Credentials(ptp_api_user="user-id", ptp_api_key="secret-key"),
    )
    requests = []

    def fake_get(url, *, params, headers, timeout):
        requests.append((url, params, headers, timeout))
        return FakeResponse(
            payload={
                "GroupId": "56",
                "ImdbId": "1234567",
                "Torrents": [
                    {"Id": "34", "ReleaseName": "Movie-GROUP", "Size": "1234",
                     "Seeders": "3", "Leechers": "1"},
                ],
            }
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    result = client.fetch_replacement("56", "34")

    assert result.torrent_id == "34"
    assert result.group_id == "56"
    assert requests == [
        (
            "https://passthepopcorn.me/torrents.php",
            {"id": "56", "torrentid": "34", "json": "1"},
            {"ApiUser": "user-id", "ApiKey": "secret-key"},
            client.config.timeout_seconds,
        )
    ]
