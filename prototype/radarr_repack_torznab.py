#!/usr/bin/env python3
"""Disposable Torznab prototype for validating Radarr REPACK upgrade behavior.

This intentionally does not modify ptp-unregistered-cleaner runtime behavior. It serves
one synthetic release whose title is the configured release title plus a REPACK/REPACKn
suffix. Use it to confirm Radarr accepts repeated same-quality revision upgrades and
performs its normal qBittorrent/import/hardlink lifecycle.
"""

from __future__ import annotations

import argparse
import html
import http.server
import json
import logging
import os
import socketserver
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

LOGGER = logging.getLogger("radarr-repack-torznab")


@dataclass(frozen=True)
class PrototypeConfig:
    host: str
    port: int
    title: str
    generation: int
    guid: str
    size: int
    category: int
    seeders: int
    peers: int
    torrent_file: Path | None
    download_url: str | None
    imdb_id: str | None
    tmdb_id: str | None

    @property
    def revision_suffix(self) -> str:
        return "REPACK" if self.generation <= 1 else f"REPACK{self.generation}"

    @property
    def synthetic_title(self) -> str:
        return f"{self.title}.{self.revision_suffix}"


CAPS_XML = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<caps>
  <server version=\"1.0\" title=\"Radarr REPACK Prototype\" />
  <limits max=\"100\" default=\"100\" />
  <searching>
    <search available=\"yes\" supportedParams=\"q\" />
    <movie-search available=\"yes\" supportedParams=\"q,imdbid,tmdbid\" />
  </searching>
  <categories>
    <category id=\"2000\" name=\"Movies\">
      <subcat id=\"2040\" name=\"Movies/HD\" />
    </category>
  </categories>
</caps>
"""


def build_feed(config: PrototypeConfig, base_url: str) -> bytes:
    pub_date = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    link = config.download_url or f"{base_url}/download/{urllib.parse.quote(config.guid)}"
    attrs = [
        ("category", str(config.category)),
        ("size", str(config.size)),
        ("seeders", str(config.seeders)),
        ("peers", str(config.peers)),
    ]
    if config.imdb_id:
        attrs.append(("imdbid", config.imdb_id.removeprefix("tt")))
    if config.tmdb_id:
        attrs.append(("tmdbid", config.tmdb_id))

    attr_xml = "\n".join(
        f'      <torznab:attr name="{html.escape(name)}" value="{html.escape(value)}" />'
        for name, value in attrs
    )
    title = html.escape(config.synthetic_title)
    guid = html.escape(config.guid)
    link_escaped = html.escape(link)

    xml = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<rss version=\"2.0\" xmlns:torznab=\"http://torznab.com/schemas/2015/feed\">
  <channel>
    <title>Radarr REPACK Prototype</title>
    <description>Single synthetic REPACK release for Radarr testing</description>
    <link>{html.escape(base_url)}</link>
    <item>
      <title>{title}</title>
      <guid isPermaLink=\"false\">{guid}</guid>
      <link>{link_escaped}</link>
      <comments>{link_escaped}</comments>
      <pubDate>{pub_date}</pubDate>
      <enclosure url=\"{link_escaped}\" length=\"{config.size}\" type=\"application/x-bittorrent\" />
{attr_xml}
    </item>
  </channel>
</rss>
"""
    return xml.encode("utf-8")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "RadarrRepackPrototype/1.0"

    @property
    def cfg(self) -> PrototypeConfig:
        return self.server.config  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: object) -> None:
        LOGGER.info("%s - %s", self.client_address[0], fmt % args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
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
            body = json.dumps(
                {
                    "ok": True,
                    "title": self.cfg.synthetic_title,
                    "generation": self.cfg.generation,
                }
            ).encode("utf-8")
            self._send(200, body, "application/json")
            return

        if parsed.path == "/api":
            mode = (query.get("t") or ["search"])[0]
            if mode == "caps":
                self._send(200, CAPS_XML.encode("utf-8"), "application/xml; charset=utf-8")
                return
            if mode in {"search", "movie"}:
                host = self.headers.get("Host") or f"localhost:{self.cfg.port}"
                proto = self.headers.get("X-Forwarded-Proto", "http")
                base_url = f"{proto}://{host}"
                self._send(200, build_feed(self.cfg, base_url), "application/rss+xml; charset=utf-8")
                return
            self._send(400, b"Unsupported Torznab query type\n", "text/plain; charset=utf-8")
            return

        if parsed.path.startswith("/download/"):
            if self.cfg.download_url:
                self.send_response(302)
                self.send_header("Location", self.cfg.download_url)
                self.end_headers()
                return
            if self.cfg.torrent_file is None:
                self._send(
                    501,
                    b"No torrent configured. Set --torrent-file or --download-url.\n",
                    "text/plain; charset=utf-8",
                )
                return
            try:
                body = self.cfg.torrent_file.read_bytes()
            except OSError as exc:
                self._send(500, f"Unable to read torrent: {exc}\n".encode(), "text/plain; charset=utf-8")
                return
            self._send(200, body, "application/x-bittorrent")
            return

        self._send(404, b"Not found\n", "text/plain; charset=utf-8")


class PrototypeServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], config: PrototypeConfig):
        self.config = config
        super().__init__(server_address, Handler)


def parse_args(argv: list[str]) -> PrototypeConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("PROTOTYPE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PROTOTYPE_PORT", "9697")))
    parser.add_argument("--title", required=True, help="Real release title without REPACK suffix")
    parser.add_argument("--generation", type=int, default=1, help="1=>REPACK, 2=>REPACK2, ...")
    parser.add_argument("--guid", default="ptp-replacement-prototype")
    parser.add_argument("--size", type=int, default=10_000_000_000)
    parser.add_argument("--category", type=int, default=2040)
    parser.add_argument("--seeders", type=int, default=10)
    parser.add_argument("--peers", type=int, default=10)
    parser.add_argument("--torrent-file", type=Path)
    parser.add_argument("--download-url")
    parser.add_argument("--imdb-id")
    parser.add_argument("--tmdb-id")
    ns = parser.parse_args(argv)

    if ns.generation < 1:
        parser.error("--generation must be >= 1")
    if ns.torrent_file and ns.download_url:
        parser.error("use only one of --torrent-file or --download-url")

    return PrototypeConfig(
        host=ns.host,
        port=ns.port,
        title=ns.title.rstrip("."),
        generation=ns.generation,
        guid=ns.guid,
        size=ns.size,
        category=ns.category,
        seeders=ns.seeders,
        peers=ns.peers,
        torrent_file=ns.torrent_file,
        download_url=ns.download_url,
        imdb_id=ns.imdb_id,
        tmdb_id=ns.tmdb_id,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = parse_args(argv or sys.argv[1:])
    server = PrototypeServer((cfg.host, cfg.port), cfg)
    LOGGER.info("Serving %s on http://%s:%s/api", cfg.synthetic_title, cfg.host, cfg.port)
    LOGGER.info("Torznab caps: http://%s:%s/api?t=caps", cfg.host, cfg.port)
    if not cfg.torrent_file and not cfg.download_url:
        LOGGER.warning("No torrent configured: search/acceptance testing only; grabs will fail intentionally")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
