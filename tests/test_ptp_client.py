import pytest

from ptp_unregistered_cleaner.ptp_client import PtpClientError, extract_unregistered_torrents


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
