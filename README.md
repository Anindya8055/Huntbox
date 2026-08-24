# Huntbox

Pull Product Hunt's top-voted launches for any time window, then work out who's
behind each one — company domain, a short description, and a contact email.

Two stages:

1. **Rank** — Product Hunt's GraphQL v2 API, ordered by votes, for a daily /
   weekly / monthly / yearly window (or an explicit date range).
2. **Enrich** — Serper.dev searches to resolve each product's real company
   domain and find a publicly listed email.

FastAPI backend, one hand-written HTML/CSS/JS frontend, no build step.

---

## Setup

Requires **Python 3.11+**.

```bash
python -m venv .venv
```

Activate it — Windows PowerShell:

```bash
.\.venv\Scripts\Activate.ps1
```

macOS / Linux:

```bash
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

## Environment variables

Copy the example file and fill in both keys:

```bash
cp .env.example .env
```

| Variable | Required | What it's for | Where to get it |
|---|---|---|---|
| `PRODUCTHUNT_API_TOKEN` | yes | Stage 1 — reading ranked launches | [producthunt.com/v2/oauth/applications](https://www.producthunt.com/v2/oauth/applications) → create an application → copy the **Developer Token** (not the API Key or Secret) |
| `SERPER_API_KEY` | for Stage 2 | Company + email lookup | [serper.dev](https://serper.dev) → sign up → dashboard → API key (2,500 free searches) |
| `AHREFS_API_KEY` | no | Domain Rating badge next to each website | [ahrefs.com/api](https://ahrefs.com/api) — uses the free `domain-rating-free` endpoint |
| `APIFY_API_TOKEN` | no | Primary domain + email resolver — crawls each product's real site via the Contact Info Scraper actor | [apify.com](https://apify.com) → Settings → Integrations → Personal API token |
| `SERPER_CONCURRENCY` | no | Parallel Serper calls, default `3` | — |
| `SERPER_DELAY_SECONDS` | no | Pause between Serper calls, default `0.35` | — |
| `APIFY_CONCURRENCY` | no | Parallel Apify actor runs, default `2` | — |
| `DOMAIN_AGE_CONCURRENCY` | no | Parallel RDAP lookups, default `2` | — |
| `HUNTBOX_DB` | no | SQLite file path, default `data/huntbox.db` | — |
| `LOG_LEVEL` | no | `DEBUG` / `INFO` / `WARNING`, default `INFO` | — |

`.env` is gitignored. Nothing is hardcoded — keys are read through
`python-dotenv` at startup.

Without `PRODUCTHUNT_API_TOKEN` the app still runs and tells you what's missing.
Without `SERPER_API_KEY` Stage 1 works and Stage 2 is skipped with a banner.
Without `APIFY_API_TOKEN`, Stage 2 still runs on Serper alone — Apify is an
additive upgrade, not a requirement.

## Running it

```bash
uvicorn app.main:app --reload --port 8000
```

Open <http://127.0.0.1:8000>. Pick a timeframe, pick a count, hit **Hunt**.
Results stream in as they're enriched.

## How the timeframes resolve

All ranges are inclusive of both endpoints and anchored to **Pacific time**
(`America/Los_Angeles`, DST-aware) — the same day boundary Product Hunt's own
leaderboard uses, not UTC midnight. Using UTC would shift which launches land
in "today" by up to eight hours and produce a different ranked set than the
live site.

Every non-custom window is the **current** period, matching what
producthunt.com's leaderboard shows:

| Mode | Range |
|---|---|
| `daily` | Today |
| `weekly` | This Monday through today |
| `monthly` | The 1st of this month through today |
| `yearly` | January 1st through today |
| `custom` | Your `date_from` / `date_to` |

Ranks inside an unfinished window are provisional and will shift until it
closes — the same as on the site.

**Product Hunt hides upvote counts for roughly the first four hours of each
launch day.** During that window every post reports zero votes, so a
votes-ordered list can't match the live board. Huntbox detects this and shows
a banner rather than pretending the order is final. Re-run later in the day for
settled ranks.

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/scrape` | Start a run. Body: `{ "timeframe": "daily", "limit": 10 }`, or `{"timeframe":"custom","date_from":"2026-01-01","date_to":"2026-01-31"}`. Returns `202` with a `job_id`. |
| `GET /api/scrape/{job_id}/status` | Poll progress. Returns state, counts, and the results so far — each item carries its own `enrichment_status`. |
| `GET /api/export?format=csv\|json` | Download the last completed run. Add `&job_id=...` to export an earlier one. |
| `GET /api/runs` | Recent completed runs, newest first. |
| `GET /api/health` | Whether each key is configured. |

Each result: `rank`, `product_name`, `tagline`, `description`, `votes`,
`comments`, `producthunt_url`, `website_url`, `company_name`,
`company_description`, `domain`, `domain_rating`, `domain_age_years`, `email`,
`email_verified`, `topics`, `launch_date`.

`domain_rating` is Ahrefs' 0–100 Domain Rating for the resolved domain, or
`null` when no domain was confirmed or no Ahrefs key is set.

`domain_age_years` is years since the domain's RDAP registration date, or
`null` when unknown, not looked up, or the registry redacts the date. It's a
helpful outreach signal, not an authoritative record — see [About match
rates](#about-match-rates).

## Persistence

Completed runs are written to SQLite at `data/huntbox.db` (override with
`HUNTBOX_DB`), so they survive a restart. On startup the most recent run is
rehydrated — you can still export it, and old job ids stay pollable.

Only *completed* runs are stored, in one transaction at the end. A run
interrupted mid-flight leaves nothing behind rather than a half-populated row.
The 50 most recent runs are kept; older ones are trimmed automatically, results
included. If the database is unwritable the run still succeeds and the failure
is logged — persistence never breaks a finished hunt.

The database file is gitignored. Delete it to start clean.

## About match rates

**Email coverage is partial by design, and always will be.** Huntbox only finds
addresses that Google has already publicly indexed — a company that doesn't
publish a contact address anywhere crawlable will come back empty. In practice
expect roughly a third to a half of launches to yield an email. A blank field
means "not found", never a guess: **no address is ever fabricated or
pattern-generated.**

Two things are worth understanding about how results are qualified.

**Product Hunt hides the real website.** Every outbound URL the API returns is a
`producthunt.com/r/...` tracking redirect, and following it hits a Cloudflare
bot challenge. So the actual company domain is unknown at the start of Stage 2.

With `APIFY_API_TOKEN` set, Huntbox points the
[Apify Contact Info Scraper](https://apify.com/vdrmota/contact-info-scraper)
actor directly at that redirect — its browser/proxy can follow the Cloudflare
hop to the real site and crawl it (home, about, contact pages) for emails,
which finds far more than a search snippet ever will. When Apify finds
nothing (or the token isn't set), Huntbox falls back to a Serper search that
recovers the domain, then checks the result actually resembles the product
name. This matters: without that check, a search for "Toplify" happily
returns `threads.com`, and you'd end up mailing the wrong company.

**Each email carries a confidence state**, shown on the card and exported as
`email_verified`:

| State | Meaning |
|---|---|
| ✓ **found** (`true`) | The address sits on the company domain we verified. Trustworthy. |
| ⚠ **unverified** (`false`, email present) | An address matching the product's name, but no company domain could be confirmed. Might belong to a similarly named company — check before using. |
| ○ **no email** (`false`, email empty) | Nothing publicly indexed. |

## Swapping the enrichment provider

Stage 2 sits behind the `EnrichmentProvider` protocol in
`app/enrichment/base.py` — `available()`, `enrich(product)`, `aclose()`.
`SerperProvider` is one implementation; a direct-site scraper or a
Hunter.io-style service can be dropped in by implementing the same three
methods and passing it to `registry.start()`. Stage 1 and the API layer don't
change.

## Tests

```bash
pytest
```

117 tests, none of which touch the network — both HTTP clients are driven by
`httpx.MockTransport` with canned payloads. Coverage focuses on the two areas
easiest to get subtly wrong: timeframe boundary maths (leap years, year
rollovers, week starts) and email extraction/validation against realistic
noisy search snippets.

## Project layout

```
app/
  main.py              FastAPI routes, export endpoints
  config.py            .env loading, structured logging
  models.py            Pydantic request/result/job models
  timeframes.py        Date-range resolution
  producthunt.py       Stage 1 — GraphQL client, cursor pagination
  jobs.py              Job registry, background runner
  storage.py           SQLite persistence for completed runs
  enrichment/
    base.py            EnrichmentProvider protocol
    serper.py          Stage 2 — Serper provider
    ahrefs.py          Domain Rating lookup
    emails.py          Email regex, validation, ranking
  templates/index.html
  static/css/huntbox.css
  static/js/huntbox.js
tests/
```
