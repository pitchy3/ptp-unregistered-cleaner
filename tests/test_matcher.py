from ptp_unregistered_cleaner.config import MatchingConfig
from ptp_unregistered_cleaner.matcher import Match, find_matches, remove_matches, torrent_allowed
from ptp_unregistered_cleaner.ptp_client import UnregisteredTorrent
from ptp_unregistered_cleaner.qbittorrent_client import Torrent, Tracker


class FakeClient:
    def __init__(self, tracker_urls: list[str] | None = None) -> None:
        self.deleted: list[str] = []
        self.tracker_urls = tracker_urls or ["https://passthepopcorn.me/announce"]

    def get_trackers(self, torrent_hash: str) -> list[Tracker]:
        return [Tracker(url=url) for url in self.tracker_urls]

    def delete_torrent(self, torrent_hash: str) -> None:
        self.deleted.append(torrent_hash)


def test_matching_qbittorrent_hashes_to_ptp_hashes() -> None:
    torrents = [Torrent(hash="ABC", name="match"), Torrent(hash="DEF", name="miss")]
    ptp = {"abc": UnregisteredTorrent(infohash="abc")}
    matches, skipped = find_matches("main", torrents, ptp, MatchingConfig())
    assert [match.torrent.name for match in matches] == ["match"]
    assert skipped == []


def test_dry_run_does_not_call_delete() -> None:
    client = FakeClient()
    matches = [Match("main", Torrent(hash="abc", name="movie"), UnregisteredTorrent("abc"))]
    removed, skipped = remove_matches(
        client,
        matches,
        dry_run=True,
        max_deletes_per_run=25,
        require_tracker_contains="passthepopcorn",
    )
    assert removed == []
    assert skipped == []
    assert client.deleted == []


def test_category_and_tag_include_exclude_logic() -> None:
    torrent = Torrent(hash="abc", name="movie", category="movies", tags=["ptp", "archive"])
    assert torrent_allowed(torrent, MatchingConfig(include_categories=["movies"])) == (True, None)
    assert torrent_allowed(torrent, MatchingConfig(exclude_categories=["movies"]))[0] is False
    assert torrent_allowed(torrent, MatchingConfig(include_tags=["ptp"])) == (True, None)
    assert torrent_allowed(torrent, MatchingConfig(exclude_tags=["archive"]))[0] is False
    assert torrent_allowed(torrent, MatchingConfig(include_tags=["missing"]))[0] is False


def test_live_delete_respects_tracker_requirement_and_cap() -> None:
    client = FakeClient()
    matches = [
        Match("main", Torrent(hash="abc", name="one"), UnregisteredTorrent("abc")),
        Match("main", Torrent(hash="def", name="two"), UnregisteredTorrent("def")),
    ]
    removed, skipped = remove_matches(
        client,
        matches,
        dry_run=False,
        max_deletes_per_run=1,
        require_tracker_contains="passthepopcorn",
    )
    assert [match.torrent.hash for match in removed] == ["abc"]
    assert skipped[0][1] == "max_deletes_per_run cap reached (1)"
    assert client.deleted == ["abc"]


def test_tracker_verification_failure_skips_delete() -> None:
    client = FakeClient(["https://example.invalid/announce"])
    matches = [Match("main", Torrent(hash="abc", name="movie"), UnregisteredTorrent("abc"))]
    removed, skipped = remove_matches(
        client,
        matches,
        dry_run=False,
        max_deletes_per_run=25,
        require_tracker_contains="passthepopcorn",
    )
    assert removed == []
    assert skipped[0][1].startswith("tracker verification failed")
    assert client.deleted == []
