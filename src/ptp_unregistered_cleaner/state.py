"""Best-effort JSON state persistence."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


@dataclass
class State:
    last_successful_run_at: str | None = None
    last_seen_infohashes_count: int = 0
    removed_hashes_by_instance: dict[str, list[str]] = field(default_factory=dict)
    skipped_hashes: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> State:
        return cls(
            last_successful_run_at=data.get("last_successful_run_at"),
            last_seen_infohashes_count=int(data.get("last_seen_infohashes_count", 0)),
            removed_hashes_by_instance={
                str(key): [str(item) for item in value]
                for key, value in (data.get("removed_hashes_by_instance", {}) or {}).items()
                if isinstance(value, list)
            },
            skipped_hashes=[
                {str(key): str(value) for key, value in item.items()}
                for item in (data.get("skipped_hashes", []) or [])
                if isinstance(item, dict)
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_successful_run_at": self.last_successful_run_at,
            "last_seen_infohashes_count": self.last_seen_infohashes_count,
            "removed_hashes_by_instance": self.removed_hashes_by_instance,
            "skipped_hashes": self.skipped_hashes,
        }


def load_state(path: str | Path) -> State:
    state_path = Path(path)
    if not state_path.exists():
        return State()
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Unable to read state file %s: %s", state_path, exc)
        return State()
    if not isinstance(data, dict):
        return State()
    return State.from_dict(data)


def save_state(path: str | Path, state: State) -> None:
    state_path = Path(path)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(state.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
    except OSError as exc:
        LOGGER.error("Unable to write state file %s: %s", state_path, exc)


def successful_state(
    *,
    infohash_count: int,
    removed_hashes_by_instance: dict[str, list[str]],
    skipped_hashes: list[dict[str, str]],
) -> State:
    return State(
        last_successful_run_at=datetime.now(UTC).isoformat(),
        last_seen_infohashes_count=infohash_count,
        removed_hashes_by_instance=removed_hashes_by_instance,
        skipped_hashes=skipped_hashes,
    )
