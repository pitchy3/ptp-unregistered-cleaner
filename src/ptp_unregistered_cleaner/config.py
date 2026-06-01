"""Configuration loading and validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - fallback for minimal test environments
    yaml = None

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    """Raised when configuration or required environment variables are invalid."""


@dataclass(frozen=True)
class AppConfig:
    interval_days: float = 3
    run_on_startup: bool = True
    dry_run: bool = True
    max_deletes_per_run: int = 25
    state_path: str = "/data/state.json"


@dataclass(frozen=True)
class PtpConfig:
    base_url: str = "https://passthepopcorn.me"
    unregistered_path: str = "/userhistory.php"
    timeout_seconds: float = 30
    min_interval_seconds_between_pages: float = 5
    page_parameter: str = "page"


@dataclass(frozen=True)
class MatchingConfig:
    require_tracker_contains: str = "passthepopcorn"
    include_categories: list[str] = field(default_factory=list)
    exclude_categories: list[str] = field(default_factory=list)
    include_tags: list[str] = field(default_factory=list)
    exclude_tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class QBittorrentConfig:
    name: str
    url: str
    username: str
    password: str


@dataclass(frozen=True)
class Credentials:
    ptp_api_user: str
    ptp_api_key: str


@dataclass(frozen=True)
class Config:
    app: AppConfig
    ptp: PtpConfig
    matching: MatchingConfig
    qbittorrent: list[QBittorrentConfig]
    credentials: Credentials


def interpolate_env(value: Any, environ: dict[str, str] | None = None) -> Any:
    """Recursively replace ${VAR_NAME} strings with values from the environment."""
    env = os.environ if environ is None else environ
    if isinstance(value, dict):
        return {key: interpolate_env(item, env) for key, item in value.items()}
    if isinstance(value, list):
        return [interpolate_env(item, env) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env:
            raise ConfigError(f"Missing environment variable referenced in config: {name}")
        return env[name]

    return _ENV_PATTERN.sub(replace, value)


def require_env(name: str, environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    value = env.get(name)
    if value is None or value == "":
        raise ConfigError(f"Required environment variable is missing: {name}")
    return value


def _as_str_list(raw: Any, field_name: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ConfigError(f"{field_name} must be a list of strings")
    return raw


def load_config(path: str | Path, environ: dict[str, str] | None = None) -> Config:
    """Load YAML config, interpolate environment variables, and validate required secrets."""
    env = os.environ if environ is None else environ
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file does not exist: {config_path}")

    raw = _load_yaml(config_path.read_text(encoding="utf-8"))
    data = interpolate_env(raw, env)

    app_raw = data.get("app", {}) or {}
    ptp_raw = data.get("ptp", {}) or {}
    matching_raw = data.get("matching", {}) or {}
    qbit_raw = data.get("qbittorrent", []) or []

    if not isinstance(qbit_raw, list) or not qbit_raw:
        raise ConfigError("At least one qbittorrent instance must be configured")

    app = AppConfig(
        interval_days=float(app_raw.get("interval_days", 3)),
        run_on_startup=bool(app_raw.get("run_on_startup", True)),
        dry_run=bool(app_raw.get("dry_run", True)),
        max_deletes_per_run=int(app_raw.get("max_deletes_per_run", 25)),
        state_path=str(app_raw.get("state_path", "/data/state.json")),
    )
    if app.interval_days <= 0:
        raise ConfigError("app.interval_days must be greater than zero")
    if app.max_deletes_per_run < 0:
        raise ConfigError("app.max_deletes_per_run must be zero or greater")

    ptp = PtpConfig(
        base_url=str(ptp_raw.get("base_url", "https://passthepopcorn.me")).rstrip("/"),
        unregistered_path=str(ptp_raw.get("unregistered_path", "/userhistory.php")),
        timeout_seconds=float(ptp_raw.get("timeout_seconds", 30)),
        min_interval_seconds_between_pages=float(
            ptp_raw.get("min_interval_seconds_between_pages", 5)
        ),
        page_parameter=str(ptp_raw.get("page_parameter", "page")),
    )

    matching = MatchingConfig(
        require_tracker_contains=str(
            matching_raw.get("require_tracker_contains", "passthepopcorn")
        ),
        include_categories=_as_str_list(
            matching_raw.get("include_categories", []), "include_categories"
        ),
        exclude_categories=_as_str_list(
            matching_raw.get("exclude_categories", []), "exclude_categories"
        ),
        include_tags=_as_str_list(matching_raw.get("include_tags", []), "include_tags"),
        exclude_tags=_as_str_list(matching_raw.get("exclude_tags", []), "exclude_tags"),
    )

    instances = []
    for index, item in enumerate(qbit_raw):
        if not isinstance(item, dict):
            raise ConfigError(f"qbittorrent entry {index} must be a mapping")
        missing = [key for key in ("name", "url", "username", "password") if not item.get(key)]
        if missing:
            raise ConfigError(f"qbittorrent entry {index} is missing: {', '.join(missing)}")
        instances.append(
            QBittorrentConfig(
                name=str(item["name"]),
                url=str(item["url"]).rstrip("/"),
                username=str(item["username"]),
                password=str(item["password"]),
            )
        )

    credentials = Credentials(
        ptp_api_user=require_env("PTP_API_USER", env),
        ptp_api_key=require_env("PTP_API_KEY", env),
    )
    return Config(
        app=app, ptp=ptp, matching=matching, qbittorrent=instances, credentials=credentials
    )


def sanitized_config_summary(config: Config) -> dict[str, Any]:
    """Return a secret-free configuration summary safe for debug logging."""
    return {
        "app": config.app,
        "ptp": config.ptp,
        "matching": config.matching,
        "qbittorrent": [
            {"name": item.name, "url": item.url, "username": "***", "password": "***"}
            for item in config.qbittorrent
        ],
        "credentials": {"ptp_api_user": "***", "ptp_api_key": "***"},
    }



def _load_yaml(text: str) -> dict[str, Any]:
    """Load YAML, using PyYAML when available and a tiny fallback for simple tests."""
    if yaml is not None:
        return yaml.safe_load(text) or {}

    # This fallback intentionally supports only the small subset used by tests.
    result: dict[str, Any] = {}
    current_section: str | None = None
    current_list_item: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if indent == 0 and line.endswith(":"):
            current_section = line[:-1]
            result[current_section] = [] if current_section == "qbittorrent" else {}
            current_list_item = None
            continue
        if current_section is None:
            continue
        if line.startswith("- "):
            current_list_item = {}
            result[current_section].append(current_list_item)  # type: ignore[union-attr]
            key, value = line[2:].split(":", 1)
            current_list_item[key.strip()] = _parse_scalar(value.strip())
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            target: dict[str, Any]
            if current_list_item is not None and indent >= 4:
                target = current_list_item
            else:
                target = result[current_section]  # type: ignore[assignment]
            target[key.strip()] = _parse_scalar(value.strip())
    return result


def _parse_scalar(value: str) -> Any:
    if value == "":
        return ""
    if value == "[]":
        return []
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    is_double_quoted = value.startswith('"') and value.endswith('"')
    is_single_quoted = value.startswith("'") and value.endswith("'")
    if is_double_quoted or is_single_quoted:
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        return value
