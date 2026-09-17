# Radarr REPACK/Torznab prototype

Disposable validation tool for the proposed PTP trump-replacement workflow. It does **not** change the cleaner's normal behavior. Python 3.9+; no third-party packages.

## Proven

Interactive testing against Radarr has shown that synthetic `.REPACK` and `.REPACK2` titles preserve the original quality and are recognized as revisions. `Do Not Upgrade Automatically` rejects them as `Repack downloading is disabled`; `Prefer and Upgrade` accepts them.

## Run a search-only test

```bash
python3 prototype/radarr_repack_torznab.py \
  --title 'Movie.2024.1080p.WEB-DL.DDP5.1.H.264-GROUP' \
  --generation 1 \
  --imdb-id tt1234567
```

Server listens on `0.0.0.0:9697`. Health/caps:

```bash
curl 'http://HOST_IP:9697/health'
curl 'http://HOST_IP:9697/api?t=caps'
```

Add a Generic Torznab indexer directly to Radarr using `http://HOST_IP:9697/api`. Use an address reachable from the Radarr container.

## Full lifecycle test with an actual PTP torrent

The prototype can now proxy PTP's real torrent download endpoint. It follows Radarr's own PTP implementation: `torrents.php?action=download&id=<torrent id>` with `ApiUser` and `ApiKey` request headers. Credentials remain server-side and are never emitted in Torznab XML or URLs.

Export the same API credentials used by `ptp-unregistered-cleaner`:

```bash
export PTP_API_USER='your-api-user'
export PTP_API_KEY='your-api-key'
```

Then run:

```bash
python3 prototype/radarr_repack_torznab.py \
  --title 'REAL.REPLACEMENT.RELEASE.NAME-GROUP' \
  --generation 1 \
  --imdb-id tt1234567 \
  --ptp-torrent-id 1234567 \
  --guid ptp-test-1234567
```

The `--title` must describe the **actual content in that PTP torrent**. The prototype only changes the Torznab-facing title by appending `.REPACK`; it does not rename the torrent payload or downloaded files.

Before involving Radarr, test the proxy itself:

```bash
curl -f -o /tmp/ptp-test.torrent 'http://127.0.0.1:9697/download/ptp-test-1234567'
ls -lh /tmp/ptp-test.torrent
```

The server log should say `Fetched PTP torrent id=...`. The downloaded file should be a real `.torrent`. Delete `/tmp/ptp-test.torrent` after checking it.

For the controlled Radarr test, temporarily set **Propers and Repacks = Prefer and Upgrade**, perform Interactive Search, and manually grab only the synthetic result. Observe:

1. Radarr requests the prototype `/download/...` URL.
2. Prototype authenticates to PTP and returns the real `.torrent`.
3. Radarr sends it to qBittorrent.
4. qBittorrent completes it.
5. Radarr Completed Download Handling imports/hardlinks it and replaces the previous library file.
6. Radarr's movie History identifies the grab/import as the synthetic REPACK revision.

Return the global proper/repack setting to its prior value after the controlled test.

## Chained replacement test

After generation 1 imports successfully, restart with generation 2 and a new GUID:

```text
--generation 2 --guid ptp-test-generation-2
```

Interactive Search should accept `.REPACK2` over the imported `.REPACK`. Then testing generation 1 again should not represent an upgrade over generation 2.

## Other download modes

A local `.torrent` can still be served with `--torrent-file /path/file.torrent`, or an existing URL can be used with `--download-url URL`. Only one download mode may be configured at a time.

## Success criteria before production implementation

1. REPACK keeps the same underlying quality but is accepted as a higher revision.
2. Radarr owns the grab and sends it to qBittorrent normally.
3. Completed Download Handling imports/hardlinks the replacement and replaces the old library file.
4. REPACK2 upgrades the already-imported REPACK release.
5. A lower revision cannot replace a higher one.

Once these pass, move the proven pieces into `ptp-unregistered-cleaner`: replacement parsing, persistent trump-chain generation, replacement-only Torznab, authenticated PTP torrent proxying, movie mapping, and a targeted Radarr search trigger.
