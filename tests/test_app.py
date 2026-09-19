import logging
from collections import Counter
from pathlib import Path

from ptp_unregistered_cleaner import app
from ptp_unregistered_cleaner.config import (
    AppConfig,
    Config,
    Credentials,
    MatchingConfig,
    PtpConfig,
    QBittorrentConfig,
    RadarrConfig,
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


def _radarr_route() -> RadarrConfig:
    return RadarrConfig(
        name="movies",
        url="http://radarr",
        api_key="key",
        qbittorrent_instances=["main"],
        torznab_external_url="http://cleaner:9697",
        torznab_api_key="proxy-key",
    )


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
        [_radarr_route()],
        {"movies": FailingCoordinator()},
        _TrackerClient(),
        "passthepopcorn",
        False,
        25,
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
        [_radarr_route()],
        {"movies": UnexpectedCoordinator()},
        _TrackerClient(),
        "passthepopcorn",
        False,
        25,
        {"movies|old-hash": "30"},
        [],
    )
    assert removable == [match]


def test_replacements_are_isolated_by_category_and_radarr_target() -> None:
    routes = [
        RadarrConfig(
            name="hd",
            url="http://hd",
            api_key="key",
            qbittorrent_instances=["shared"],
            qbittorrent_categories=["radarr"],
            torznab_external_url="http://cleaner:9697",
            torznab_api_key="hd-key",
        ),
        RadarrConfig(
            name="uhd",
            url="http://uhd",
            api_key="key",
            qbittorrent_instances=["shared"],
            qbittorrent_categories=["radarr-4k"],
            torznab_port=9698,
            torznab_external_url="http://cleaner:9698",
            torznab_api_key="uhd-key",
        ),
    ]
    calls: dict[str, list[str]] = {"hd": [], "uhd": []}

    class Coordinator:
        def __init__(self, target: str) -> None:
            self.target = target

        def replace(self, match: app.Match) -> None:
            calls[self.target].append(match.torrent.category)

    matches = [
        app.Match(
            "shared",
            Torrent("hd-hash", "HD", "radarr"),
            UnregisteredTorrent(
                "hd-hash", torrent_id="10", group_id="20", reason="30"
            ),
        ),
        app.Match(
            "shared",
            Torrent("uhd-hash", "UHD", "radarr-4k"),
            UnregisteredTorrent(
                "uhd-hash", torrent_id="11", group_id="21", reason="31"
            ),
        ),
    ]
    requests = {}
    removable = app._request_replacements(
        matches,
        routes,
        {"hd": Coordinator("hd"), "uhd": Coordinator("uhd")},
        _TrackerClient(),
        "passthepopcorn",
        False,
        25,
        requests,
        [],
    )
    assert removable == matches
    assert calls == {"hd": ["radarr"], "uhd": ["radarr-4k"]}
    assert requests == {"hd|hd-hash": "30", "uhd|uhd-hash": "31"}


def test_unmapped_category_does_not_use_a_radarr_coordinator() -> None:
    match = app.Match(
        "main",
        Torrent("old-hash", "Old.Release", "manual"),
        UnregisteredTorrent(
            "old-hash", torrent_id="10", group_id="20", reason="30"
        ),
    )
    removable = app._request_replacements(
        [match],
        [
            RadarrConfig(
                name="movies",
                url="http://radarr",
                api_key="key",
                qbittorrent_instances=["main"],
                qbittorrent_categories=["radarr"],
                torznab_external_url="http://cleaner:9697",
                torznab_api_key="proxy-key",
            )
        ],
        {},
        _TrackerClient(),
        "passthepopcorn",
        False,
        25,
        {},
        [],
    )
    assert removable == [match]


def test_zero_cleanup_cap_does_not_request_replacement() -> None:
    match = app.Match(
        "main",
        Torrent("old-hash", "Old.Release"),
        UnregisteredTorrent(
            "old-hash", torrent_id="10", group_id="20", reason="30"
        ),
    )

    class UnexpectedCoordinator:
        def replace(self, _match: app.Match) -> None:
            raise AssertionError("replacement must honor the cleanup cap")

    skipped = []
    removable = app._request_replacements(
        [match],
        [_radarr_route()],
        {"movies": UnexpectedCoordinator()},
        _TrackerClient(),
        "passthepopcorn",
        False,
        0,
        {},
        skipped,
    )
    assert removable == []
    assert skipped[0][1] == "max_deletes_per_run cap reached (0)"


def test_replacement_checkpoint_waits_for_all_instance_copies() -> None:
    requests = {"old-hash": "30"}
    remaining = Counter({"old-hash": 1})

    app._prune_replacement_requests(requests, remaining, True)
    assert requests == {"old-hash": "30"}

    remaining["old-hash"] = 0
    app._prune_replacement_requests(requests, remaining, True)
    assert requests == {}


def test_replacement_checkpoint_survives_offline_instance() -> None:
    requests = {"old-hash": "30"}
    app._prune_replacement_requests(requests, Counter(), False)
    assert requests == {"old-hash": "30"}


def test_failed_checkpoint_write_preserves_replaced_torrent(
    tmp_path: Path, monkeypatch
) -> None:
    deleted: list[str] = []

    class Ptp:
        def fetch_unregistered(self):
            return {
                "old-hash": UnregisteredTorrent(
                    "old-hash", torrent_id="10", group_id="20", reason="30"
                )
            }

    class Qbit:
        def __init__(self, _config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def list_torrents(self):
            return [Torrent("old-hash", "Old.Release", "radarr")]

        def get_trackers(self, _torrent_hash):
            return [Tracker("https://passthepopcorn.me/announce")]

        def delete_torrent(self, torrent_hash):
            deleted.append(torrent_hash)

    class Coordinator:
        def replace(self, _match):
            return "30"

    route = RadarrConfig(
        name="movies",
        url="http://radarr",
        api_key="key",
        qbittorrent_instances=["main"],
        qbittorrent_categories=["radarr"],
        torznab_external_url="http://cleaner:9697",
        torznab_api_key="proxy-key",
    )
    config = Config(
        app=AppConfig(dry_run=False, state_path=str(tmp_path / "state.json")),
        ptp=PtpConfig(),
        matching=MatchingConfig(),
        qbittorrent=[QBittorrentConfig("main", "http://main", "user", "pass")],
        credentials=Credentials("api-user", "api-key"),
        radarr=[route],
    )
    monkeypatch.setattr(app, "QBittorrentClient", Qbit)
    monkeypatch.setattr(app, "save_state", lambda *_args, **_kwargs: False)

    app.run_once(config, ptp_client=Ptp(), coordinators={"movies": Coordinator()})

    assert deleted == []


def test_failed_checkpoint_write_preserves_later_instance_copy(
    tmp_path: Path, monkeypatch
) -> None:
    deleted: list[tuple[str, str]] = []
    replacement_calls: list[str] = []

    class Ptp:
        def fetch_unregistered(self):
            return {
                "old-hash": UnregisteredTorrent(
                    "old-hash", torrent_id="10", group_id="20", reason="30"
                )
            }

    class Qbit:
        def __init__(self, config):
            self.instance = config.name

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def list_torrents(self):
            return [Torrent("old-hash", "Old.Release", "radarr")]

        def get_trackers(self, _torrent_hash):
            return [Tracker("https://passthepopcorn.me/announce")]

        def delete_torrent(self, torrent_hash):
            deleted.append((self.instance, torrent_hash))

    class Coordinator:
        def replace(self, match):
            replacement_calls.append(match.instance_name)
            return "30"

    route = RadarrConfig(
        name="movies",
        url="http://radarr",
        api_key="key",
        qbittorrent_instances=["first", "second"],
        qbittorrent_categories=["radarr"],
        torznab_external_url="http://cleaner:9697",
        torznab_api_key="proxy-key",
    )
    config = Config(
        app=AppConfig(dry_run=False, state_path=str(tmp_path / "state.json")),
        ptp=PtpConfig(),
        matching=MatchingConfig(),
        qbittorrent=[
            QBittorrentConfig("first", "http://first", "user", "pass"),
            QBittorrentConfig("second", "http://second", "user", "pass"),
        ],
        credentials=Credentials("api-user", "api-key"),
        radarr=[route],
    )
    monkeypatch.setattr(app, "QBittorrentClient", Qbit)
    monkeypatch.setattr(app, "save_state", lambda *_args, **_kwargs: False)

    app.run_once(config, ptp_client=Ptp(), coordinators={"movies": Coordinator()})

    assert replacement_calls == ["first"]
    assert deleted == []
