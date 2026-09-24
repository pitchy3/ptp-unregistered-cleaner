"""Small, replacement-only Torznab server used by Radarr."""

from __future__ import annotations

import html
import http.server
import logging
import secrets
import threading
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime

from .ptp_client import PtpClient, ReplacementTorrent

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublishedReplacement:
    replacement: ReplacementTorrent

    @property
    def guid(self) -> str:
        return f"ptp-replacement-{self.replacement.torrent_id}"


class ReplacementCatalog:
    def __init__(self) -> None:
        self._entries: dict[str, PublishedReplacement] = {}
        self._lock = threading.Lock()

    def publish(self, replacement: ReplacementTorrent) -> PublishedReplacement:
        entry = PublishedReplacement(replacement)
        with self._lock:
            self._entries[entry.guid] = entry
        return entry

    def get(self, guid: str) -> PublishedReplacement | None:
        with self._lock:
            return self._entries.get(guid)

    def discard(self, guid: str) -> None:
        """Stop exposing a replacement after its explicit Radarr grab attempt."""
        with self._lock:
            self._entries.pop(guid, None)

    def entries(self) -> list[PublishedReplacement]:
        with self._lock:
            return list(self._entries.values())


CAPS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<caps><server version="1.0" title="PTP Replacement Cleaner" />
<limits max="100" default="100" /><searching>
<search available="yes" supportedParams="q" />
<movie-search available="yes" supportedParams="q,imdbid,tmdbid" />
</searching><categories><category id="2000" name="Movies" /></categories></caps>
"""


def build_feed(
    entries: list[PublishedReplacement], external_url: str, api_key: str
) -> bytes:
    items = []
    pub_date = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    for entry in entries:
        replacement = entry.replacement
        link = (
            f"{external_url}/download/{urllib.parse.quote(entry.guid)}?"
            + urllib.parse.urlencode({"apikey": api_key})
        )
        attrs = [
            ("category", "2000"),
            ("size", str(replacement.size)),
            ("seeders", str(replacement.seeders)),
            ("peers", str(replacement.peers)),
        ]
        if replacement.imdb_id:
            attrs.append(("imdbid", replacement.imdb_id.removeprefix("tt")))
        if replacement.tmdb_id:
            attrs.append(("tmdbid", replacement.tmdb_id))
        attr_xml = "".join(
            f'<torznab:attr name="{html.escape(name)}" value="{html.escape(value)}" />'
            for name, value in attrs
        )
        items.append(
            f"<item><title>{html.escape(replacement.title)}</title>"
            f'<guid isPermaLink="false">{html.escape(entry.guid)}</guid>'
            f"<link>{html.escape(link)}</link><pubDate>{pub_date}</pubDate>"
            f'<enclosure url="{html.escape(link)}" length="{replacement.size}" '
            f'type="application/x-bittorrent" />{attr_xml}</item>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">'
        "<channel><title>PTP Replacement Cleaner</title>"
        f"<link>{html.escape(external_url)}</link>{''.join(items)}</channel></rss>"
    ).encode()


def build_validation_feed(external_url: str) -> bytes:
    """Return one intentionally invalid release for Radarr's connection test.

    Radarr validates Torznab indexers with an identifier-less movie/RSS query and
    rejects an otherwise valid empty feed. That request is indistinguishable from
    an RSS poll, so the placeholder deliberately has an empty title and a
    non-existent download URL: Radarr's connection test sees one parsed item,
    while normal release filtering rejects it before it can become downloadable.
    """
    pub_date = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    placeholder_url = f"{external_url}/validation-placeholder"
    escaped_url = html.escape(placeholder_url)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">'
        "<channel><title>PTP Replacement Cleaner</title>"
        "<item><title></title>"
        '<guid isPermaLink="false">ptp-replacement-validation-placeholder</guid>'
        f"<link>{escaped_url}</link><pubDate>{pub_date}</pubDate>"
        f'<enclosure url="{escaped_url}" length="0" type="application/x-bittorrent" />'
        '<torznab:attr name="category" value="2000" />'
        "</item></channel></rss>"
    ).encode()


def matching_movie_entries(
    entries: list[PublishedReplacement], imdb_id: str, tmdb_id: str
) -> list[PublishedReplacement]:
    """Expose candidates only to an identifier-scoped Radarr movie search."""
    normalized_imdb = imdb_id.casefold().removeprefix("tt")
    normalized_tmdb = tmdb_id.strip()
    if not normalized_imdb and not normalized_tmdb:
        return []
    return [
        entry
        for entry in entries
        if (
            not normalized_imdb
            or (entry.replacement.imdb_id or "").casefold().removeprefix("tt")
            == normalized_imdb
        )
        and (
            not normalized_tmdb
            or (entry.replacement.tmdb_id or "").strip() == normalized_tmdb
        )
    ]


class ReplacementServer:
    def __init__(
        self,
        host: str,
        port: int,
        external_url: str,
        api_key: str,
        catalog: ReplacementCatalog,
        ptp_client: PtpClient,
    ) -> None:
        self.external_url = external_url
        self.api_key = api_key
        self.catalog = catalog
        self.ptp_client = ptp_client
        handler = self._handler()
        self._server = http.server.ThreadingHTTPServer((host, port), handler)
        self._thread: threading.Thread | None = None

    def _handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                # BaseHTTPRequestHandler includes the full query string, including the
                # proxy API key, in its default access log. Never emit it.
                LOGGER.info("Torznab request completed for %s", self.client_address[0])

            def send_body(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                if parsed.path == "/health":
                    self.send_body(200, b'{"ok":true}', "application/json")
                    return
                if parsed.path == "/api":
                    if not secrets.compare_digest(
                        query.get("apikey", [""])[0], owner.api_key
                    ):
                        self.send_body(401, b"Unauthorized", "text/plain")
                        return
                    mode = query.get("t", ["search"])[0]
                    if mode == "caps":
                        self.send_body(200, CAPS_XML, "application/xml")
                    elif mode in {"search", "movie"}:
                        entries = []
                        response_body = None
                        if mode == "movie":
                            imdb_id = query.get("imdbid", [""])[0]
                            tmdb_id = query.get("tmdbid", [""])[0]
                            if imdb_id or tmdb_id:
                                entries = matching_movie_entries(
                                    owner.catalog.entries(), imdb_id, tmdb_id
                                )
                            elif not query.get("q", [""])[0]:
                                response_body = build_validation_feed(owner.external_url)
                        self.send_body(
                            200,
                            response_body
                            or build_feed(entries, owner.external_url, owner.api_key),
                            "application/rss+xml",
                        )
                    else:
                        self.send_body(400, b"Unsupported query", "text/plain")
                    return
                prefix = "/download/"
                if parsed.path.startswith(prefix):
                    if not secrets.compare_digest(
                        query.get("apikey", [""])[0], owner.api_key
                    ):
                        self.send_body(401, b"Unauthorized", "text/plain")
                        return
                    entry = owner.catalog.get(urllib.parse.unquote(parsed.path[len(prefix) :]))
                    if entry is None:
                        self.send_body(404, b"Unknown replacement", "text/plain")
                        return
                    try:
                        body = owner.ptp_client.download_torrent(
                            entry.replacement.torrent_id
                        )
                    except Exception as exc:  # converted to a safe proxy failure
                        LOGGER.error("PTP torrent proxy failed: %s", exc)
                        self.send_body(502, b"PTP torrent proxy failed", "text/plain")
                        return
                    self.send_body(200, body, "application/x-bittorrent")
                    return
                self.send_body(404, b"Not found", "text/plain")

        return Handler

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> ReplacementServer:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
