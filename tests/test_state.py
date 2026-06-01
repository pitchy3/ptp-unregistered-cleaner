from pathlib import Path

from ptp_unregistered_cleaner.state import State, load_state, save_state, successful_state


def test_state_json_write_read(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = State(
        last_successful_run_at="2026-01-01T00:00:00+00:00",
        last_seen_infohashes_count=2,
        removed_hashes_by_instance={"main": ["abc"]},
        skipped_hashes=[{"instance": "main", "hash": "def", "reason": "cap"}],
    )
    save_state(path, state)
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
