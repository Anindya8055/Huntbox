# Huntbox — project notes for Claude

Internal SEO-outreach tool: pulls top Product Hunt launches, resolves each
company's real domain + a contact email, so the team can pitch link-building/
SEO services to leads with weak DR. FastAPI backend, one hand-written HTML/
CSS/JS frontend (no build step, no framework). See [README.md](README.md) for
the full user-facing docs — this file is deployment/history notes that don't
belong there.

## Architecture at a glance

- `app/producthunt.py` — Stage 1, GraphQL v2, ranked by votes.
- `app/timeframes.py` — day/week/month/year window resolution, anchored to
  **Pacific time** (`zoneinfo`, DST-aware) to match Product Hunt's own
  leaderboard day boundary — NOT UTC. Get this wrong and ranks silently
  drift from what producthunt.com shows.
- `app/enrichment/` — Stage 2, domain + email resolution:
  - `apify.py` — **primary** resolver. Points Apify's `vdrmota/contact-info-scraper`
    actor at each product's PH redirect URL. A real browser is required here:
    Cloudflare blocks a plain `httpx`/`curl` redirect-follow with a `403` +
    `Cf-Mitigated: challenge` even with a realistic User-Agent (verified
    directly — there is no cheap code-level workaround). Composes a
    `SerperProvider` as automatic fallback.
  - `serper.py` — Google-search-based domain discovery, used standalone when
    `APIFY_API_TOKEN` isn't set, and as Apify's fallback. Structurally misses
    indie/personal domains that don't rank in Google's top 8 or whose label
    doesn't fuzzy-match the product name (e.g. a domain named after the
    founder, not the product).
  - `domain_age.py` — free RDAP lookup (`rdap.org`, follow_redirects required —
    it's a bootstrap redirector).
  - `ahrefs.py` — free Domain Rating endpoint.
- `app/jobs.py` — background job registry; `_enrich_all` runs all products
  concurrently via `asyncio.gather`.
- `app/storage.py` — SQLite persistence. **Known wart**: `load_run()` always
  reconstructs a restored job with `state="done"`, even if the underlying run
  never actually finished. Only matters if you poll a job sparsely — the
  real frontend polls every 700ms so this is invisible in normal use, but a
  single manual status check can catch a mid-flight job and misreport it as
  "done, 0 results." Worth fixing properly at some point (return an honest
  state instead of hardcoding).
- `app/main.py` — routes. `settings` is a module-level singleton
  (`app.config.Settings`, frozen dataclass); `PATCH /api/settings` reassigns
  the global after an atomic `.env` rewrite so changes apply without a
  restart — see below.

## Live API-key settings (`PATCH /api/settings`)

Added so the Apify/Serper tokens can be rotated from the deployed UI instead
of editing `.env` by hand. **Deliberately has no auth** — the whole app has
zero authentication anywhere, and the user explicitly chose write-only
fields (never echo the current token back) as the only mitigation, not a
login gate. Don't add auth to just this endpoint without discussing it —
it'd be inconsistent with the rest of the app.

`app/config.py`'s `update_env_file()` does the atomic rewrite (temp file +
`os.replace()`, preserves unrelated lines/comments/order) and updates
`os.environ` directly so the very next `get_settings()` call picks it up.

## Frontend defaults

`app/static/js/huntbox.js`'s `DEFAULTS` object controls what's visible out of
the box. **Don't let `drMax` (or any other filter default) narrow below the
requested hunt Limit** — a fetched-and-enriched result should always be
visible by default; filters exist to narrow manually, not to pre-hide
anything. This bit us once already (`drMax: 20` silently hid results above
DR 20, so "Limit 10" could show as few as 7 rows).

Cache-busting: `index.html`'s CSS/JS `<link>`/`<script>` tags carry
`?v={{ asset_version }}`, stamped from process start time in `main.py`. This
exists because the production LiteSpeed server sends
`Cache-Control: public, max-age=604800` on `/static/*` — without the version
query param, a browser with a warm cache won't see a deployed CSS/JS change
for up to a week. If you ever see "I deployed but the site looks unchanged,"
check this first — it's almost always a stale asset cache, not a failed
deploy (confirmed once already: fetching the exact same URL with a
cache-busting query param immediately showed the new file was already live
on the server).

## Production deployment (HostArmada / cPanel)

- **No SSH** from outside the account's allow-listed IPs — reachable only
  through cPanel's Terminal app or File Manager, both browser-based (use
  Claude in Chrome, not the Bash tool, which has no path to this host).
- **App root is NOT the domain's docroot.** `hunter.yaaplylink.com`'s own
  folder holds nothing but `.htaccess`; the actual app lives at
  `/home/yaaplyco/huntbox`, wired via CloudLinux Passenger config in that
  `.htaccess` (`PassengerAppRoot`, `PassengerPython` pointing at
  `/home/yaaplyco/virtualenv/huntbox/3.13/bin/python`).
- **`passenger_wsgi.py` on the server is hand-rolled** — wraps the FastAPI
  ASGI app with `a2wsgi.ASGIMiddleware`, and rebuilds the app object whenever
  the OS pid changes (`_state["pid"] != pid`) to survive Passenger forking a
  new worker. Never overwrite this file from a local copy — it doesn't exist
  in this repo and isn't meant to; if it ever needs changing, edit it in
  place on the server.
- **Server's `requirements.txt` differs from local on purpose**: no
  `uvicorn`/`pytest`/`pytest-asyncio` (dev-only), but it needs `a2wsgi` (not
  present in the local dev `requirements.txt` since local dev runs plain
  `uvicorn`, no WSGI shim). Never blindly overwrite the server's
  `requirements.txt` with the local one — diff first, then hand-edit /
  append just the line(s) that actually changed (e.g. adding `tzdata`).
- **`.env`, `passenger_wsgi.py`, and `data/huntbox.db` must never be touched
  by a deploy** — back up `app/` + `requirements.txt` before any redeploy
  (`tar czf ~/huntbox-app-backup-<date>.tar.gz app requirements.txt`), then
  upload a zip of just the changed files via File Manager, extract with
  `unzip -o`, clear `__pycache__`, and `touch tmp/restart.txt` to restart
  Passenger. cPanel's own "Run Pip Install" button in Setup Python App has
  been unreliable (silently no-ops) — install new deps directly via
  `/home/yaaplyco/virtualenv/huntbox/3.13/bin/pip install -r requirements.txt`
  from Terminal instead.
- **cPanel's Terminal websocket drops mid-session frequently** — if a
  command's output never appears, click Reconnect and retype rather than
  assuming it ran.
- Health-check after every deploy: `curl -s https://hunter.yaaplylink.com/api/health`
  should reflect the new config immediately (it's a good signal the process
  actually restarted and the new code is running).

## Testing conventions

- `tests/` has no `TestClient` usage prior to `test_settings.py` — that file
  introduced the pattern (`from fastapi.testclient import TestClient`) for
  endpoint-level tests. When testing anything that reads `os.environ`/`.env`,
  isolate with `monkeypatch.delenv(...)` first — the real dev `.env`'s values
  leak into `os.environ` for the whole test process otherwise.
- 190 tests as of this writing, all network-mocked (`httpx.MockTransport`) or
  filesystem-isolated (`tmp_path`) — none touch the real Product Hunt/Serper/
  Apify/Ahrefs APIs or the real `.env`.
