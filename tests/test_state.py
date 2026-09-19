from pathlib import Path

import pytest

from ptp_unregistered_cleaner import state as state_module
from ptp_unregistered_cleaner.state import (
    State,
    StateError,
    load_state,
    save_state,
    successful_state,
)


def test_state_json_write_read(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = State(
        last_successful_run_at="2026-01-01T00:00:00+00:00",
        last_seen_infohashes_count=2,
        removed_hashes_by_instance={"main": ["abc"]},
        skipped_hashes=[{"instance": "main", "hash": "def", "reason": "cap"}],
        replacement_requests={"old-hash": "123"},
    )
    assert save_state(path, state) is True
    loaded = load_state(path)
    assert loaded == state


def test_successful_state_sets_expected_fields() -> None:
    state = successful_state(
        infohash_count=3,
        removed_hashes_by_instance={"main": ["abc"]},
        skipped_hashes=[],
    )
    assert state.last_successful_run_at is not None
    assert state.last_seen_infohashes_count == 3
    assert state.removed_hashes_by_instance == {"main": ["abc"]}


def test_state_write_reports_failure(tmp_path: Path) -> None:
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("occupied", encoding="utf-8")
    assert save_state(parent_file / "state.json", State()) is False


def test_failed_atomic_replace_preserves_previous_state(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "state.json"
    original = State(replacement_requests={"movies|old": "30"})
    assert save_state(path, original) is True

    def fail_replace(_source, _destination):
        raise OSError("disk full")

    monkeypatch.setattr(state_module.os, "replace", fail_replace)
    replacement = State(replacement_requests={"movies|new": "31"})
    assert save_state(path, replacement) is False
    assert load_state(path) == original
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_invalid_existing_state_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(StateError, match="Unable to read state file"):
        load_state(path)


@pytest.mark.parametrize("malformed", [None, [], ["movies|old", "30"]])
def test_malformed_replacement_checkpoint_map_fails_closed(
    tmp_path: Path, malformed
) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        state_module.json.dumps({"replacement_requests": malformed}),
        encoding="utf-8",
    )
    with pytest.raises(StateError, match="replacement_requests must be a JSON object"):
        load_state(path)
