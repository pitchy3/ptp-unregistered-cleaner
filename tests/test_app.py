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
from ptp_unregistered_cleaner.qbittorrent_client import QBittorrentClientError


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
