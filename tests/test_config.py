from pathlib import Path

import pytest

from ptp_unregistered_cleaner.config import ConfigError, interpolate_env, load_config


def test_environment_interpolation() -> None:
    assert interpolate_env({"password": "${SECRET}"}, {"SECRET": "value"}) == {"password": "value"}


def test_missing_interpolated_env_var_errors() -> None:
    with pytest.raises(ConfigError, match="MISSING"):
        interpolate_env("${MISSING}", {})


def test_load_config_requires_ptp_credentials(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "qbittorrent:\n"
        "  - name: main\n"
        "    url: http://example\n"
        "    username: user\n"
        "    password: pass\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="PTP_API_USER"):
        load_config(config, {})


def test_load_config_interpolates_qbit_credentials(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "app:\n  dry_run: true\n"
        "qbittorrent:\n"
        "  - name: main\n"
        "    url: http://example\n"
        "    username: ${QBIT_USER}\n"
        "    password: ${QBIT_PASS}\n",
        encoding="utf-8",
    )
    loaded = load_config(
        config,
        {
            "PTP_API_USER": "api-user",
            "PTP_API_KEY": "api-key",
            "QBIT_USER": "user",
            "QBIT_PASS": "pass",
        },
    )
    assert loaded.qbittorrent[0].username == "user"
    assert loaded.qbittorrent[0].password == "pass"
    assert loaded.app.dry_run is True


def test_enabled_radarr_requires_proxy_and_api_secrets(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "radarr:\n  enabled: true\n  url: http://radarr:7878\n"
        "qbittorrent:\n"
        "  - name: main\n    url: http://example\n    username: user\n    password: pass\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="api_key.*torznab_external_url.*torznab_api_key"):
        load_config(config, {"PTP_API_USER": "user", "PTP_API_KEY": "key"})
