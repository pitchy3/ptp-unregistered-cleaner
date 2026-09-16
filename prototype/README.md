# Radarr REPACK/Torznab prototype

This is a disposable validation tool for the proposed PTP trump-replacement workflow. It does **not** change the cleaner's normal behavior.

It exposes one synthetic Torznab movie result. Generation 1 appends `.REPACK`, generation 2 `.REPACK2`, etc. The purpose is to prove that current Radarr treats successive same-quality releases as native revision upgrades and then performs its normal download/import/hardlink/replacement workflow.

## 1. Run the server

Requires Python 3.9+ and no third-party packages.

From a clone of this branch:

```bash
python3 prototype/radarr_repack_torznab.py \
  --title 'Movie.2024.1080p.WEB-DL.DDP5.1.H.264-GROUP' \
  --generation 1 \
  --imdb-id tt1234567
```

It listens on `0.0.0.0:9697`. Check it from another machine/container that can reach it:

```bash
curl 'http://HOST_IP:9697/health'
curl 'http://HOST_IP:9697/api?t=caps'
```

Use an address reachable **from the Radarr container**. `localhost` inside Radarr normally points to Radarr itself, not the host running this prototype.

## 2. Add it directly to Radarr

For the first test, bypass Prowlarr to reduce variables.

In Radarr, add a **Generic Torznab** indexer and set its URL/base URL to:

```text
http://HOST_IP:9697/api
```

If Radarr's Generic Torznab form expects a base host rather than the full API path in your build, use `http://HOST_IP:9697` and verify that its test request reaches `/api?t=caps` in the prototype log.

No API key is required. Enable movie search. The prototype advertises Movies/HD (2040).

Before testing, verify Radarr's **Propers and Repacks** behavior is not configured to `Do Not Prefer`; the intended test uses Radarr's native revision-upgrade path.

## 3. Acceptance-only test (safe first test)

Start the prototype **without** `--torrent-file` or `--download-url`. Perform an interactive search for the matching movie in Radarr.

Expected result:

- the synthetic release appears;
- quality/source/resolution remain the same as the real title;
- `.REPACK` makes it an accepted revision upgrade rather than a same-quality rejection.

Do not click Grab in this mode. The download endpoint deliberately returns HTTP 501.

If Radarr rejects the release, inspect the rejection reason before proceeding. This is the most important result of the prototype.

## 4. Full lifecycle test

Use a disposable/test movie and a **real torrent that actually contains the release represented by `--title`**. Do not use an unrelated torrent: Radarr's completed-download parsing/import must correspond to the downloaded content.

Serve a local torrent file:

```bash
python3 prototype/radarr_repack_torznab.py \
  --title 'Movie.2024.1080p.WEB-DL.DDP5.1.H.264-GROUP' \
  --generation 1 \
  --imdb-id tt1234567 \
  --guid ptp-test-1 \
  --torrent-file /path/to/replacement.torrent
```

Or redirect the download endpoint to an already-authenticated/test torrent URL:

```bash
python3 prototype/radarr_repack_torznab.py \
  --title 'Movie.2024.1080p.WEB-DL.DDP5.1.H.264-GROUP' \
  --generation 1 \
  --imdb-id tt1234567 \
  --guid ptp-test-1 \
  --download-url 'https://example.invalid/path/to/test.torrent'
```

Search interactively in Radarr and grab the synthetic result. Confirm Radarr sends it to the configured download client, Completed Download Handling imports it, and your normal hardlink workflow is used.

## 5. Critical chained-replacement test

After generation 1 has imported successfully, stop the server and restart it with:

```text
--generation 2 --guid ptp-test-2
```

and a second valid replacement torrent/title as appropriate. Search the same movie again.

Expected result: `.REPACK2` is accepted as an upgrade over the imported `.REPACK` revision and Radarr performs the replacement again.

Then optionally restart with generation 1 again. It should **not** be accepted as an upgrade over generation 2.

## Success criteria

The production implementation should not begin until these are observed:

1. REPACK keeps the same underlying quality but is accepted as a higher revision.
2. Radarr owns the grab and sends it to qBittorrent normally.
3. Completed Download Handling imports/hardlinks the replacement and replaces the old library file.
4. REPACK2 upgrades the already-imported REPACK release.
5. A lower revision cannot replace a higher one.

Once these pass, the proven pieces can be moved into `ptp-unregistered-cleaner`: persistent trump-chain generation, a replacement-only Torznab endpoint, authenticated PTP torrent proxying, movie mapping, and a Radarr `MoviesSearch` trigger.
