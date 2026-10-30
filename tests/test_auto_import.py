from copy import deepcopy

import pytest

from ptp_unregistered_cleaner.auto_import import advance_import, poll_import_jobs


def job():
    return {
        "movie_id": 290,
        "original_movie_file_id": 19,
        "guid": "ptp-replacement-1551269",
        "route": "movies",
        "replacement_id": "1551269",
        "phase": "pending",
    }


def candidate():
    return {
        "path": "/downloads/Little.Miss.Sunshine.2006.1080p.mkv",
        "folderName": "Little.Miss.Sunshine",
        "downloadId": "abc123",
        "size": 1000000,
        "movie": {"id": 290},
        "quality": {"quality": {"id": 3}},
        "languages": [{"id": 1}],
        "releaseGroup": "TEST",
        "indexerFlags": 0,
        "rejections": [
            {"reason": "Not an upgrade for existing movie file. "
                       "Existing quality: Bluray-1080p. New Quality WEBRip-1080p."}
        ],
    }


class FakeRadarr:
    def __init__(self):
        self.record = candidate()
        self.posted = []
        self.download_id = "abc123"
        self.file_id = 19
        self.status = "completed"

    def movie(self, movie_id):
        return {"id": movie_id, "movieFileId": self.file_id}

    def grabbed_download_id(self, movie_id, guid):
        assert movie_id == 290
        assert guid == "ptp-replacement-1551269"
        return self.download_id

    def queue_for_movie(self, movie_id):
        return [{"movieId": movie_id, "downloadId": self.download_id,
                 "status": self.status, "sizeleft": 0,
                 "outputPath": "/downloads"}]

    def manual_import(self, download_id):
        assert download_id == self.download_id
        return [self.record]

    def submit_manual_import(self, payload):
        self.posted.append(payload)
        return 17

    def download_was_imported(self, movie_id, download_id):
        return movie_id == 290 and download_id == self.download_id

    def command(self, command_id):
        return {"id": command_id, "status": "completed"}


def test_import_submits_only_after_durable_intent():
    client = FakeRadarr()
    item = job()
    states = []

    def checkpoint(updated):
        states.append(deepcopy(updated))

    assert advance_import(client, item, checkpoint)
    assert [s["phase"] for s in states] == ["submitting", "submitted"]
    assert len(client.posted) == 1
    assert client.posted[0]["downloadId"] == "abc123"
    assert client.posted[0]["movieId"] == 290
    assert not advance_import(client, item, checkpoint)
    assert len(client.posted) == 1
    client.file_id = 20
    assert advance_import(client, item, checkpoint)
    assert item["phase"] == "completed"


@pytest.mark.parametrize(
    "change",
    [
        {"movie": {"id": 999}},
        {"downloadId": "other"},
        {"rejections": [{"reason": "File is corrupt"}]},
        {"rejections": [
            {"reason": "Not an upgrade for existing movie file. "
                       "Existing quality: Bluray-1080p. New Quality WEBRip-1080p."},
            {"reason": "Unknown movie"},
        ]},
        {"rejections": []},
        {"path": "/elsewhere/other"},
        {"languages": []},
    ],
)
def test_unsafe_candidates_never_submit(change):
    client = FakeRadarr()
    client.record.update(change)
    item = job()
    assert not advance_import(client, item, lambda _item: None)
    assert not client.posted
    assert item["phase"] == "pending"


def test_waiting_does_not_submit():
    client = FakeRadarr()
    client.status = "downloading"
    assert not advance_import(client, job(), lambda _item: None)
    assert not client.posted


def test_uncertain_submissions_do_not_resend():
    client = FakeRadarr()
    item = job()
    item.update(phase="submitting", download_id="abc123", source_path="/downloads/a")
    assert not advance_import(client, item, lambda _item: None)
    assert not client.posted


def test_persistence_failure_prevents_post():
    client = FakeRadarr()
    item = job()

    def fail(_item):
        raise RuntimeError("cannot save")

    with pytest.raises(RuntimeError, match="cannot save"):
        advance_import(client, item, fail)
    assert not client.posted


def test_multiple_pending_jobs_for_one_movie_never_submit():
    from ptp_unregistered_cleaner.config import RadarrConfig

    first = job()
    second = job()
    second["replacement_id"] = "another"
    second["guid"] = "ptp-replacement-another"
    jobs = {"first": first, "second": second}
    routes = [
        RadarrConfig(
            name="movies", url="http://example.invalid", api_key="key",
            qbittorrent_instances=["main"], auto_import_replacements=True,
        )
    ]
    poll_import_jobs(
        routes, jobs,
        lambda _key, _job: pytest.fail("Ambiguous jobs must not checkpoint"),
    )
    assert first["phase"] == second["phase"] == "pending"
