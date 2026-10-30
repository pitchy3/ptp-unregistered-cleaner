"""Safely reconcile imported PTP replacements using Radarr only."""

from __future__ import annotations

import logging
import re
from typing import Any

from .config import RadarrConfig
from .radarr_client import RadarrClient, RadarrClientError
from .state import StateError

LOGGER = logging.getLogger(__name__)

_DOWNGRADE = re.compile(
    r"not an upgrade for existing movie file\. existing quality: "
    r"[a-z0-9+_. -]+\. new quality [a-z0-9+_. -]+\.",
    re.IGNORECASE,
)


def _download_candidate(
    client: RadarrClient, job: dict[str, Any]
) -> tuple[dict[str, Any], str] | None:
    download_id = client.grabbed_download_id(job["movie_id"], job["guid"])
    if not download_id:
        return None
    matches = [
        item for item in client.queue_for_movie(job["movie_id"])
        if item.get("movieId") == job["movie_id"]
        and str(item.get("downloadId") or "").casefold() == download_id.casefold()
    ]
    if len(matches) != 1:
        return None
    item = matches[0]
    if item.get("status") != "completed" or item.get("sizeleft") != 0:
        return None
    return item, str(item["downloadId"])


def _candidate_is_safe(
    item: dict[str, Any], job: dict[str, Any], download_id: str
) -> bool:
    movie = item.get("movie")
    rejections = item.get("rejections")
    if (
        not isinstance(movie, dict)
        or movie.get("id") != job["movie_id"]
        or item.get("downloadId") != download_id
        or not isinstance(item.get("path"), str)
        or not item["path"].startswith("/")
        or not isinstance(item.get("quality"), dict)
        or not isinstance(item.get("languages"), list)
        or not item["languages"]
        or not isinstance(rejections, list)
    ):
        return False
    for reason in rejections:
        if not isinstance(reason, dict) or not _DOWNGRADE.fullmatch(
            str(reason.get("reason") or "").strip()
        ):
            return False
    return bool(rejections)


def advance_import(
    client: RadarrClient, job: dict[str, Any], checkpoint
) -> bool:
    """Advance one persisted job. checkpoint(job) must durably persist before POST."""
    if job.get("phase") == "completed":
        return False
    movie = client.movie(job["movie_id"])
    if movie.get("movieFileId") != job["original_movie_file_id"]:
        if job.get("phase") in {"submitted", "submitting"}:
            job["phase"] = "completed"
            checkpoint(job)
            LOGGER.info("PTP replacement import verified: movie_id=%s", job["movie_id"])
            return True
        LOGGER.warning("Movie file changed outside import workflow; manual review needed")
        return False

    if job.get("phase") == "submitting":
        LOGGER.warning("Manual import submission outcome uncertain; manual review needed")
        return False
    if job.get("phase") == "submitted":
        command_id = job.get("command_id")
        if not isinstance(command_id, int):
            return False
        command = client.command(command_id)
        if str(command.get("status")).casefold() in {"failed", "aborted"}:
            LOGGER.warning("Radarr manual import failed: command=%s", command_id)
        return False

    if job.get("phase") != "pending":
        raise StateError("Unknown import-job phase")

    matched = _download_candidate(client, job)
    if matched is None:
        return False
    _, download_id = matched
    candidates = client.manual_import(download_id)
    if len(candidates) != 1:
        LOGGER.warning("Manual import requires exactly one candidate: movie_id=%s", job["movie_id"])
        return False
    candidate = candidates[0]
    if not _candidate_is_safe(candidate, job, download_id):
        LOGGER.warning("Manual import candidate rejected by safety validation: movie_id=%s", job["movie_id"])
        return False

    payload = {
        key: candidate[key]
        for key in ("path", "folderName", "releaseGroup", "quality", "languages", "indexerFlags")
        if key in candidate
    }
    payload["movieId"] = job["movie_id"]
    payload["downloadId"] = download_id

    job.update(phase="submitting", download_id=download_id, source_path=payload["path"])
    checkpoint(job)  # Must be durable before any manual-import POST.
    command_id = client.submit_manual_import(payload)
    job.update(phase="submitted", command_id=command_id)
    checkpoint(job)
    LOGGER.info("Submitted verified PTP manual import: movie_id=%s command_id=%s", job["movie_id"], command_id)
    return True


def poll_import_jobs(
    configs: list[RadarrConfig], jobs: dict[str, dict[str, Any]], checkpoint
) -> None:
    routes = {route.name.casefold(): route for route in configs if route.auto_import_replacements}
    for key, job in list(jobs.items()):
        route = routes.get(str(job.get("route", "")).casefold())
        if route is None or job.get("phase") == "completed":
            continue
        try:
            advance_import(
                RadarrClient(route), job,
                lambda updated, key=key: checkpoint(key, updated),
            )
        except (RadarrClientError, StateError):
            LOGGER.exception("Unable to reconcile Radarr manual import for %s", key)
