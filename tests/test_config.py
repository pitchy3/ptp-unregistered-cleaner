from pathlib import Path

import pytest

from ptp_unregistered_cleaner.config import (
    ConfigError,
    interpolate_env,
    load_config,
    radarr_route,
)


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


def test_radarr_requires_route_and_api_secrets(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "radarr:\n  - name: movies\n    url: http://radarr:7878\n"
        "qbittorrent:\n"
        "  - name: main\n    url: http://example\n    username: user\n    password: pass\n",
        encoding="utf-8",
    )
    with pytest.raises(
        ConfigError,
        match="api_key.*torznab_external_url.*torznab_api_key.*qbittorrent_instances",
    ):
        load_config(config, {"PTP_API_USER": "user", "PTP_API_KEY": "key"})


def test_radarr_routes_by_qbittorrent_instance_and_category(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "qbittorrent:\n"
        "  - name: shared\n    url: http://qbit\n    username: user\n    password: pass\n"
        "radarr:\n"
        "  - name: hd\n    url: http://hd\n    api_key: hd-key\n"
        "    qbittorrent_instances: [shared]\n"
        "    qbittorrent_categories: [radarr]\n"
        "    torznab_external_url: http://cleaner:9697\n"
        "    torznab_api_key: hd-proxy\n"
        "  - name: uhd\n    url: http://uhd\n    api_key: uhd-key\n"
        "    qbittorrent_instances: [shared]\n"
        "    qbittorrent_categories: [radarr-4k]\n"
        "    torznab_port: 9698\n"
        "    torznab_external_url: http://cleaner:9698\n"
        "    torznab_api_key: uhd-proxy\n",
        encoding="utf-8",
    )
    loaded = load_config(config, {"PTP_API_USER": "user", "PTP_API_KEY": "key"})
    assert radarr_route(loaded.radarr, "shared", "radarr").name == "hd"
    assert radarr_route(loaded.radarr, "shared", "RADARR-4K").name == "uhd"
    assert radarr_route(loaded.radarr, "shared", "other") is None


def test_ambiguous_radarr_routes_are_rejected(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "qbittorrent:\n"
        "  - name: shared\n    url: http://qbit\n    username: user\n    password: pass\n"
        "radarr:\n"
        "  - name: one\n    url: http://one\n    api_key: key\n"
        "    qbittorrent_instances: [shared]\n"
        "    torznab_external_url: http://cleaner:9697\n"
        "    torznab_api_key: proxy\n"
        "  - name: two\n    url: http://two\n    api_key: key\n"
        "    qbittorrent_instances: [shared]\n"
        "    qbittorrent_categories: [radarr-4k]\n"
        "    torznab_port: 9698\n"
        "    torznab_external_url: http://cleaner:9698\n"
        "    torznab_api_key: proxy\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Ambiguous radarr routes"):
        load_config(config, {"PTP_API_USER": "user", "PTP_API_KEY": "key"})


def test_radarr_torznab_ports_must_be_unique_across_bind_hosts(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "qbittorrent:\n"
        "  - name: hd\n    url: http://hd-qbit\n    username: user\n    password: pass\n"
        "  - name: uhd\n    url: http://uhd-qbit\n    username: user\n    password: pass\n"
        "radarr:\n"
        "  - name: hd\n    url: http://hd\n    api_key: key\n"
        "    qbittorrent_instances: [hd]\n"
        "    torznab_host: 0.0.0.0\n    torznab_port: 9697\n"
        "    torznab_external_url: http://cleaner:9697\n"
        "    torznab_api_key: hd-proxy\n"
        "  - name: uhd\n    url: http://uhd\n    api_key: key\n"
        "    qbittorrent_instances: [uhd]\n"
        "    torznab_host: 127.0.0.1\n    torznab_port: 9697\n"
        "    torznab_external_url: http://localhost:9697\n"
        "    torznab_api_key: uhd-proxy\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Multiple radarr entries use Torznab port 9697"):
        load_config(config, {"PTP_API_USER": "user", "PTP_API_KEY": "key"})
