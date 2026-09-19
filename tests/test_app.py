import logging
from pathlib import Path

from ptp_unregistered_cleaner import app
from ptp_unregistered_cleaner.config import (
    AppConfig,
    Config,
    Credentials,
    MatchingConfig,
    PtpConfig,
    QBittorrentConfig,
)
from ptp_unregistered_cleaner.ptp_client import UnregisteredTorrent
from ptp_unregistered_cleaner.qbittorrent_client import (
    QBittorrentClientError,
    Torrent,
    Tracker,
)
from ptp_unregistered_cleaner.state import load_state


class _TrackerClient:
    def get_trackers(self, _torrent_hash: str) -> list[Tracker]:
        return [Tracker("https://passthepopcorn.me/announce")]


def _config(tmp_path: Path) -> Config:
    return Config(
        app=AppConfig(state_path=str(tmp_path / "state.json")),
        ptp=PtpConfig(),
        matching=MatchingConfig(),
        qbittorrent=[
            QBittorrentConfig("offline", "http://offline", "user", "pass"),
            QBittorrentConfig("healthy", "http://healthy", "user", "pass"),
        ],
        credentials=Credentials("api-user", "api-key"),
    )


def test_run_once_skips_failed_qbittorrent_instance(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    processed_instances: list[str] = []

    class FakePtpClient:
        def __init__(self, *_args) -> None:
            pass

        def fetch_unregistered(self) -> dict:
            return {}

    class FakeQBittorrentClient:
        def __init__(self, config: QBittorrentConfig) -> None:
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def list_torrents(self) -> list:
            processed_instances.append(self.config.name)
            if self.config.name == "offline":
                raise QBittorrentClientError("connection refused")
            return []

    monkeypatch.setattr(app, "PtpClient", FakePtpClient)
    monkeypatch.setattr(app, "QBittorrentClient", FakeQBittorrentClient)
    caplog.set_level(logging.ERROR)

    app.run_once(_config(tmp_path))

    assert processed_instances == ["offline", "healthy"]
    assert (tmp_path / "state.json").exists()
    assert "qBittorrent instance offline failed; skipping this instance" in caplog.text


def test_run_once_records_deletions_completed_before_instance_failure(
    tmp_path: Path, monkeypatch
) -> None:
    class FakePtpClient:
        def __init__(self, *_args) -> None:
            pass

        def fetch_unregistered(self) -> dict[str, UnregisteredTorrent]:
            return {
                "first": UnregisteredTorrent("first"),
                "second": UnregisteredTorrent("second"),
            }

    class PartiallyFailingQBittorrentClient:
        def __init__(self, config: QBittorrentConfig) -> None:
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def list_torrents(self) -> list[Torrent]:
            return [Torrent("first", "one"), Torrent("second", "two")]

        def get_trackers(self, _torrent_hash: str) -> list[Tracker]:
            return [Tracker("https://passthepopcorn.me/announce")]

        def delete_torrent(self, torrent_hash: str) -> None:
            if torrent_hash == "second":
                raise QBittorrentClientError("connection lost")

    config = Config(
        app=AppConfig(dry_run=False, state_path=str(tmp_path / "state.json")),
        ptp=PtpConfig(),
        matching=MatchingConfig(),
        qbittorrent=[QBittorrentConfig("main", "http://main", "user", "pass")],
        credentials=Credentials("api-user", "api-key"),
    )
    monkeypatch.setattr(app, "PtpClient", FakePtpClient)
    monkeypatch.setattr(app, "QBittorrentClient", PartiallyFailingQBittorrentClient)

    app.run_once(config)

    state = load_state(tmp_path / "state.json")
    assert state.removed_hashes_by_instance == {"main": ["first"]}


def test_failed_replacement_is_preserved_for_retry() -> None:
    match = app.Match(
        "main",
        Torrent("old-hash", "Old.Release"),
        UnregisteredTorrent(
            "old-hash", torrent_id="10", group_id="20", reason="30"
        ),
    )

    class FailingCoordinator:
        def replace(self, _match: app.Match) -> None:
            raise RuntimeError("Radarr rejected it")

    skipped = []
    requests = {}
    removable = app._request_replacements(
        [match],
        FailingCoordinator(),
        _TrackerClient(),
        "passthepopcorn",
        False,
        True,
        requests,
        skipped,
    )
    assert removable == []
    assert requests == {}
    assert "preserved for retry" in skipped[0][1]


def test_checkpointed_replacement_is_not_requested_twice() -> None:
    match = app.Match(
        "main",
        Torrent("OLD-HASH", "Old.Release"),
        UnregisteredTorrent(
            "old-hash", torrent_id="10", group_id="20", reason="30"
        ),
    )

    class UnexpectedCoordinator:
        def replace(self, _match: app.Match) -> None:
            raise AssertionError("replacement should not be requested twice")

    removable = app._request_replacements(
        [match],
        UnexpectedCoordinator(),
        _TrackerClient(),
        "passthepopcorn",
        False,
        True,
        {"old-hash": "30"},
        [],
    )
    assert removable == [match]
