import logging

import pytest

from ptp_unregistered_cleaner import app
from ptp_unregistered_cleaner.config import (
    AppConfig,
    Config,
    ConfigError,
    Credentials,
    MatchingConfig,
    PtpConfig,
    QBittorrentConfig,
)


def _config() -> Config:
    return Config(
        app=AppConfig(run_on_startup=True),
        ptp=PtpConfig(),
        matching=MatchingConfig(),
        qbittorrent=[QBittorrentConfig("main", "http://main", "user", "pass")],
        credentials=Credentials("api-user", "api-key"),
    )


def test_daemon_retries_after_failed_startup_run(monkeypatch, caplog) -> None:
    class StopDaemon(Exception):
        pass

    cleanup_calls = 0

    def failed_cleanup(_config: Config, **_kwargs) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        raise RuntimeError("temporary failure")

    def stop_at_first_sleep(_seconds: float) -> None:
        raise StopDaemon

    monkeypatch.delenv("RUN_ONCE", raising=False)
    monkeypatch.setattr(app, "run_once", failed_cleanup)
    monkeypatch.setattr(app.time, "sleep", stop_at_first_sleep)
    caplog.set_level(logging.ERROR)

    with pytest.raises(StopDaemon):
        app.run_daemon(_config())

    assert cleanup_calls == 1
    assert "Cleanup run failed; daemon will retry after the configured interval" in caplog.text


def test_daemon_still_exits_for_startup_config_error(monkeypatch) -> None:
    def invalid_cleanup(_config: Config, **_kwargs) -> None:
        raise ConfigError("invalid")

    monkeypatch.delenv("RUN_ONCE", raising=False)
    monkeypatch.setattr(app, "run_once", invalid_cleanup)

    with pytest.raises(ConfigError, match="invalid"):
        app.run_daemon(_config())
