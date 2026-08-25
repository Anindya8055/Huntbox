"""Huntbox -- FastAPI application entrypoint."""

from __future__ import annotations

import csv
import io
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR, configure_logging, get_settings, update_env_file
from app.enrichment.ahrefs import AhrefsClient
from app.enrichment.apify import ApifyContactProvider
from app.enrichment.domain_age import DomainAgeClient
from app.enrichment.emails import normalize_domain
from app.enrichment.serper import SerperProvider
from app.jobs import registry
from app.models import LEAD_STATUSES, LeadUpdate, PruneRequest, ScrapeRequest, SettingsUpdate
from app.producthunt import ProductHuntClient
from app.storage import Storage
from app.timeframes import label_for, resolve_range

settings = get_settings()
configure_logging(settings.log_level)
log = logging.getLogger("huntbox")

# Cache-busting query param for /static assets. The static CSS/JS are served
# with a long browser Cache-Control by the front-end web server (LiteSpeed on
# the HostArmada deploy), so without this, a redeploy's asset changes stay
# invisible to anyone with a warm cache until it expires on its own.
ASSET_VERSION = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

storage = Storage(settings.db_path)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await registry.attach_storage(storage)
    log.info("Huntbox ready (db: %s)", settings.db_path)
    yield


app = FastAPI(title="Huntbox", version="2.0.0", lifespan=lifespan)


@app.middleware("http")
async def no_store_api(request: Request, call_next):
    """Never let a proxy or the browser serve a stale /api response.

    The Product Hunt client itself holds no cache -- every hunt opens a fresh
    connection -- so this closes the only remaining staleness vector.
    """
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

EXPORT_FIELDS = [
    "rank", "product_name", "tagline", "description", "votes", "comments",
    "producthunt_url", "website_url", "company_name", "company_description",
    "domain", "domain_rating", "domain_age_years", "email", "email_verified",
    "topics", "launch_date",
    "lead_status", "lead_note", "lead_updated_by", "lead_updated_at",
]


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "has_ph_token": settings.has_producthunt,
            "has_serper_key": settings.has_serper,
            "has_ahrefs_key": settings.has_ahrefs,
            "has_apify_key": settings.has_apify,
            "lead_statuses": LEAD_STATUSES,
            "asset_version": ASSET_VERSION,
        },
    )


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "producthunt_token": settings.has_producthunt,
        "serper_key": settings.has_serper,
        "ahrefs_key": settings.has_ahrefs,
        "apify_key": settings.has_apify,
    }


@app.patch("/api/settings")
async def update_settings(req: SettingsUpdate) -> dict:
    """Update API keys in the server's .env, live -- no restart needed.

    Write-only: a blank/omitted field means "leave unchanged", and the
    response never echoes a token value back, matching the has_* boolean
    pattern already used by /api/health.
    """
    global settings
    updates: dict[str, str] = {}
    if req.apify_api_token and req.apify_api_token.strip():
        updates["APIFY_API_TOKEN"] = req.apify_api_token.strip()
    if req.serper_api_key and req.serper_api_key.strip():
        updates["SERPER_API_KEY"] = req.serper_api_key.strip()

    if not updates:
        raise HTTPException(status_code=400, detail="Provide at least one API key to update.")

    try:
        update_env_file(BASE_DIR, updates)
    except OSError as exc:
        log.warning("Failed to write .env: %s", exc)
        raise HTTPException(status_code=503, detail="Could not save settings. Check the server logs.") from exc

    settings = get_settings()
    log.info("Settings updated: %s", ", ".join(sorted(updates)))
    return {"ok": True, "has_apify": settings.has_apify, "has_serper": settings.has_serper}


@app.post("/api/scrape")
async def scrape(req: ScrapeRequest) -> JSONResponse:
    """Kick off a run and return immediately with a job id to poll."""
    ph_client = ProductHuntClient(settings.producthunt_token)
    ok, reason = ph_client.available()
    if not ok:
        raise HTTPException(status_code=400, detail=reason)

    try:
        rng = resolve_range(req.timeframe, date_from=req.date_from, date_to=req.date_to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    provider = None
    warning = ""
    if req.enrich:
        serper_candidate = SerperProvider(
            settings.serper_api_key,
            settings.serper_concurrency,
            settings.serper_delay_seconds,
        )
        serper_usable, serper_why = serper_candidate.available()

        if settings.has_apify:
            # Apify crawls each product's real site (via PH's website_url
            # redirect) and is the primary resolver; Serper is wired in as
            # an automatic fallback when Apify finds nothing.
            provider = ApifyContactProvider(
                settings.apify_api_token,
                serper_candidate,
                settings.apify_concurrency,
            )
        elif serper_usable:
            provider = serper_candidate
        else:
            warning = serper_why

    ahrefs = AhrefsClient(settings.ahrefs_api_key) if settings.has_ahrefs else None
    domain_age = DomainAgeClient(settings.domain_age_concurrency)

    job = await registry.start(req, ph_client, provider, ahrefs, domain_age)
    return JSONResponse(
        {
            "job_id": job.job_id,
            "range_label": label_for(req.timeframe, rng),
            "warning": warning,
        },
        status_code=202,
    )


@app.get("/api/scrape/{job_id}/status")
async def job_status(job_id: str) -> dict:
    job = await registry.get_or_load(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="That run has expired. Start a new hunt.")
    return job.model_dump()


@app.get("/api/runs")
async def runs(limit: int = Query(20, ge=1, le=50)) -> dict:
    """Recent completed runs, newest first — survives restarts."""
    return {"runs": await storage.history(limit)}


@app.get("/api/leads")
async def list_leads() -> dict:
    """Every stored lead, newest touch first."""
    return {"leads": [lead.model_dump() for lead in await storage.all_leads()]}


@app.patch("/api/leads/{domain}")
async def update_lead(domain: str, body: LeadUpdate) -> dict:
    """Set status and/or note for one company domain."""
    domain = normalize_domain(domain)
    if not domain or "." not in domain:
        raise HTTPException(status_code=400, detail="A valid company domain is required.")
    if body.status is None and body.note is None:
        raise HTTPException(status_code=400, detail="Provide a status or a note to update.")

    lead = await storage.upsert_lead(
        domain, status=body.status, note=body.note, updated_by=body.updated_by.strip()
    )
    if lead is None:
        raise HTTPException(
            status_code=503, detail="Could not save that change. Check the server logs."
        )

    # Keep the in-memory run in step so a poll doesn't revert the badge.
    for job in (registry.last_completed,):
        if job is None:
            continue
        for product in job.results:
            if product.domain == domain:
                product.lead_status = lead.status
                product.lead_note = lead.note
                product.lead_updated_by = lead.updated_by
                product.lead_updated_at = lead.updated_at

    log.info("Lead %s -> %s by %s", domain, lead.status, lead.updated_by or "anon")
    return lead.model_dump()


@app.get("/api/leads/{domain}/history")
async def lead_history(domain: str, limit: int = Query(50, ge=1, le=200)) -> dict:
    """Append-only change log for one domain, newest first."""
    domain = normalize_domain(domain)
    if not domain:
        raise HTTPException(status_code=400, detail="A valid company domain is required.")
    events = await storage.lead_history(domain, limit)
    return {"domain": domain, "events": [e.model_dump() for e in events]}


@app.post("/api/leads/prune")
async def prune_leads(body: PruneRequest) -> dict:
    """Preview or delete stale, never-worked leads.

    Without `confirm: true` this only reports what *would* go, so the UI can
    show a count before anything is destroyed. Leads carrying a note are
    never eligible -- that note is someone's work.
    """
    candidates = await storage.prunable_leads(body.older_than_days, list(body.statuses))
    preview = [c.model_dump() for c in candidates[:50]]

    if not body.confirm:
        return {
            "dry_run": True,
            "eligible": len(candidates),
            "sample": preview,
            "criteria": {
                "older_than_days": body.older_than_days,
                "statuses": list(body.statuses),
                "excludes": "leads with a note are never pruned",
            },
        }

    removed = await storage.prune_leads([c.domain for c in candidates])
    log.info("Pruned %d leads older than %dd", removed, body.older_than_days)
    return {"dry_run": False, "removed": removed, "sample": preview}


@app.delete("/api/leads/{domain}")
async def delete_lead(domain: str) -> dict:
    """Forget one lead entirely, history included."""
    domain = normalize_domain(domain)
    if not domain:
        raise HTTPException(status_code=400, detail="A valid company domain is required.")
    removed = await storage.delete_lead(domain)
    if not removed:
        raise HTTPException(status_code=404, detail=f"No stored lead for {domain}.")
    return {"domain": domain, "removed": True}


@app.get("/api/export")
async def export(
    format: str = Query("csv", pattern="^(csv|json)$"),
    job_id: str | None = Query(None),
) -> Response:
    job = await registry.get_or_load(job_id) if job_id else registry.last_completed
    if job is None or not job.results:
        raise HTTPException(status_code=404, detail="Nothing to export yet — run a hunt first.")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    rows = [
        {
            **{k: getattr(p, k) for k in EXPORT_FIELDS if k != "topics"},
            "topics": ", ".join(p.topics),
        }
        for p in job.results
    ]

    if format == "json":
        body = json.dumps(rows, indent=2, ensure_ascii=False)
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="huntbox-{stamp}.json"'},
        )

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        # BOM so Excel opens UTF-8 product names correctly.
        content="﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="huntbox-{stamp}.csv"'},
    )
