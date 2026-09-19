import httpx
import pytest

from ptp_unregistered_cleaner.matcher import Match
from ptp_unregistered_cleaner.ptp_client import (
    PtpClientError,
    ReplacementTorrent,
    UnregisteredTorrent,
    extract_replacement_torrent,
)
from ptp_unregistered_cleaner.qbittorrent_client import Torrent
from ptp_unregistered_cleaner.radarr_client import RadarrClient, RadarrClientError
from ptp_unregistered_cleaner.replacement import ReplacementCoordinator
from ptp_unregistered_cleaner.torznab import (
    ReplacementCatalog,
    ReplacementServer,
    build_feed,
)


def test_reason_numeric_value_is_designated_replacement() -> None:
    row = UnregisteredTorrent("hash", torrent_id="12", reason="34")
    assert row.replacement_torrent_id == "34"
    assert UnregisteredTorrent("hash", reason="Superior Source").replacement_torrent_id is None
    assert UnregisteredTorrent("hash", torrent_id="12", reason="12").replacement_torrent_id is None


def test_extract_replacement_requires_expected_group() -> None:
    payload = {
        "response": {
            "torrent": {
                "Id": 34,
                "GroupId": 56,
                "ReleaseName": "Movie.2026.1080p.BluRay-GROUP",
                "Size": 1234,
                "Seeders": 4,
                "Leechers": 2,
            },
            "group": {"Id": 56, "ImdbId": "tt1234567", "TmdbId": 99},
        }
    }
    result = extract_replacement_torrent(payload, "56", "34")
    assert result.title == "Movie.2026.1080p.BluRay-GROUP"
    assert result.imdb_id == "tt1234567"
    assert result.tmdb_id == "99"
    assert result.seeders == 4
    assert result.peers == 6
    with pytest.raises(PtpClientError, match="not '57'"):
        extract_replacement_torrent(payload, "57", "34")


def test_feed_uses_real_title_and_server_side_download_url() -> None:
    catalog = ReplacementCatalog()
    entry = catalog.publish(
        ReplacementTorrent(
            "34", "56", "Movie.2026.1080p.BluRay-GROUP", 1234, "tt1", seeders=4, peers=6
        )
    )
    feed = build_feed(catalog.entries(), "http://cleaner:9697", "proxy-secret").decode()
    assert "Movie.2026.1080p.BluRay-GROUP" in feed
    assert "REPACK" not in feed
    assert f"http://cleaner:9697/download/{entry.guid}?apikey=proxy-secret" in feed
    assert "ApiKey" not in feed
    assert 'name="seeders" value="4"' in feed


def test_each_radarr_catalog_exposes_only_its_own_replacements() -> None:
    hd = ReplacementCatalog()
    uhd = ReplacementCatalog()
    hd.publish(
        ReplacementTorrent(
            "34", "56", "Movie.1080p-GROUP", 1234, "tt1", seeders=1, peers=1
        )
    )
    uhd.publish(
        ReplacementTorrent(
            "35", "56", "Movie.2160p-GROUP", 5678, "tt1", seeders=1, peers=1
        )
    )
    hd_feed = build_feed(hd.entries(), "http://cleaner:9697", "hd-key").decode()
    uhd_feed = build_feed(uhd.entries(), "http://cleaner:9698", "uhd-key").decode()
    assert "Movie.1080p-GROUP" in hd_feed
    assert "Movie.2160p-GROUP" not in hd_feed
    assert "Movie.2160p-GROUP" in uhd_feed
    assert "Movie.1080p-GROUP" not in uhd_feed


def test_torznab_server_requires_proxy_key_for_search_and_download() -> None:
    class Ptp:
        def download_torrent(self, torrent_id: str) -> bytes:
            assert torrent_id == "34"
            return b"d4:infode"

    catalog = ReplacementCatalog()
    entry = catalog.publish(
        ReplacementTorrent(
            "34", "56", "Movie-GROUP", 1234, "tt1", seeders=1, peers=1
        )
    )
    server = ReplacementServer(
        "127.0.0.1", 0, "http://cleaner:9697", "proxy-secret", catalog, Ptp()
    )
    port = server._server.server_address[1]
    with server, httpx.Client(trust_env=False) as client:
        assert client.get(f"http://127.0.0.1:{port}/api?t=caps").status_code == 401
        assert (
            client.get(
                f"http://127.0.0.1:{port}/api?t=caps&apikey=proxy-secret"
            ).status_code
            == 200
        )
        response = client.get(
            f"http://127.0.0.1:{port}/download/{entry.guid}?apikey=proxy-secret"
        )
        assert response.status_code == 200
        assert response.content == b"d4:infode"


def test_radarr_grab_only_bypasses_known_upgrade_policy(monkeypatch) -> None:
    client = object.__new__(RadarrClient)
    calls = []
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: calls.append((args, kwargs)))
    release = {
        "guid": "ptp-replacement-34",
        "indexerId": 10,
        "mappedMovieId": 7,
        "downloadAllowed": True,
        "rejections": ["Existing file on disk is of equal or higher preference"],
    }
    client.grab(release, 7)
    assert calls[0][0] == ("POST", "/release")

    release["rejections"] = ["Release is blocklisted"]
    with pytest.raises(RadarrClientError, match="blocklisted"):
        client.grab(release, 7)


def test_radarr_movie_requires_all_supplied_ids_to_match(monkeypatch) -> None:
    client = object.__new__(RadarrClient)
    movies = [
        {"id": 7, "imdbId": "tt1", "tmdbId": 99},
        {"id": 8, "imdbId": "tt2", "tmdbId": 100},
    ]
    monkeypatch.setattr(client, "_request", lambda *_args, **_kwargs: movies)

    assert client.find_movie("tt1", "99")["id"] == 7
    assert client.find_movie("tt1", None)["id"] == 7
    assert client.find_movie(None, "100")["id"] == 8
    with pytest.raises(RadarrClientError, match="found 0"):
        client.find_movie("tt1", "100")
    with pytest.raises(RadarrClientError, match="identifier is required"):
        client.find_movie(None, None)


def test_coordinator_verifies_and_grabs_exact_release() -> None:
    replacement = ReplacementTorrent(
        "34", "56", "Movie-GROUP", 1234, "tt1", seeders=1, peers=1
    )

    class Ptp:
        def fetch_replacement(self, group_id, torrent_id):
            assert (group_id, torrent_id) == ("56", "34")
            return replacement

    class Radarr:
        def find_movie(self, imdb_id, tmdb_id):
            assert (imdb_id, tmdb_id) == ("tt1", None)
            return {"id": 7, "hasFile": True, "movieFileId": 8}

        def find_release(self, movie_id, guid):
            assert (movie_id, guid) == (7, "ptp-replacement-34")
            return {"guid": guid}

        def grab(self, release, movie_id):
            assert (release, movie_id) == ({"guid": "ptp-replacement-34"}, 7)

    match = Match(
        "main",
        Torrent("hash", "old"),
        UnregisteredTorrent("hash", torrent_id="12", group_id="56", reason="34"),
    )
    catalog = ReplacementCatalog()
    result = ReplacementCoordinator(Ptp(), Radarr(), catalog).replace(match)
    assert result == "34"
    assert catalog.entries() == []


def test_coordinator_removes_release_from_catalog_when_radarr_rejects() -> None:
    replacement = ReplacementTorrent(
        "34", "56", "Movie-GROUP", 1234, "tt1", seeders=1, peers=1
    )

    class Ptp:
        def fetch_replacement(self, _group_id, _torrent_id):
            return replacement

    class Radarr:
        def find_movie(self, _imdb_id, _tmdb_id):
            return {"id": 7, "hasFile": True, "movieFileId": 8}

        def find_release(self, _movie_id, _guid):
            raise RadarrClientError("mapped to the wrong movie")

    match = Match(
        "main",
        Torrent("hash", "old"),
        UnregisteredTorrent("hash", torrent_id="12", group_id="56", reason="34"),
    )
    catalog = ReplacementCatalog()
    with pytest.raises(RadarrClientError, match="wrong movie"):
        ReplacementCoordinator(Ptp(), Radarr(), catalog).replace(match)
    assert catalog.entries() == []
