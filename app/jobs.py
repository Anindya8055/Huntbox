"""In-memory job registry driving the poll-based progress UI.

POST /api/scrape creates a job, kicks off an asyncio background task and
returns a job_id immediately. The frontend polls
GET /api/scrape/{job_id}/status and re-renders cards in place as products
flip from `pending` to `found` / `unverified` / `no_email`.

Polling over WebSockets on purpose: single-process, short-lived jobs, and
it survives a reconnect without extra machinery.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from datetime import date, datetime, timezone

from app.enrichment.ahrefs import AhrefsClient
from app.enrichment.base import EnrichmentProvider
from app.enrichment.domain_age import DomainAgeClient
from app.enrichment.serper import SerperError, SerperQuotaError
from app.models import JobStatus, Product, ScrapeRequest
from app.producthunt import ProductHuntClient, ProductHuntError
from app.storage import Storage
from app.timeframes import label_for, resolve_range

log = logging.getLogger("huntbox.jobs")

MAX_JOBS = 20

# A running job lives in this process's memory, but a status poll can be served
# by any worker (and the process can be recycled mid-run). So we mirror job
# state to SQLite as it changes, letting get_or_load() recover it anywhere.
# Throttled because enrichment updates far faster than anyone polls.
PERSIST_THROTTLE_SECONDS = 2.0


# Product Hunt's launch day starts at 00:01 Pacific (07:01 UTC), and upvote
# counts stay hidden for roughly the first four hours of it.
PH_DAY_START_UTC_HOUR = 7
PH_VOTES_HIDDEN_HOURS = 4


def _hidden_votes_notice(now: datetime | None = None) -> str:
    """Explain zero-vote results, and say when counts should appear."""
    now = now or datetime.now(timezone.utc)
    reveal = now.replace(
        hour=PH_DAY_START_UTC_HOUR + PH_VOTES_HIDDEN_HOURS,
        minute=0, second=0, microsecond=0,
    )
    base = (
        "Product Hunt hides upvote counts for the first few hours of each "
        "launch day (its day starts at 00:01 Pacific), so every count here "
        "reads zero and the order is provisional. "
    )
    if now < reveal:
        mins = int((reveal - now).total_seconds() // 60)
        when = f"{mins} min" if mins < 90 else f"{mins // 60}h {mins % 60:02d}m"
        return base + (
            f"Counts should appear in about {when} "
            f"({reveal.strftime('%H:%M')} UTC). Pick a completed window for "
            "settled ranks in the meantime."
        )
    return base + "Try a completed window for settled ranks."


class JobRegistry:
    """Bounded, in-process store of recent jobs plus the last completed run."""

    def __init__(self) -> None:
        self._jobs: OrderedDict[str, JobStatus] = OrderedDict()
        self._tasks: dict[str, asyncio.Task] = {}
        self._timeframes: dict[str, str] = {}
        self._last_persist: dict[str, float] = {}
        self.last_completed: JobStatus | None = None
        self.storage: Storage | None = None

    async def attach_storage(self, storage: Storage) -> None:
        """Wire up persistence and rehydrate the most recent completed run."""
        self.storage = storage
        await storage.init()
        restored = await storage.load_last_run()
        if restored is not None:
            self.last_completed = restored
            self._jobs[restored.job_id] = restored
            log.info(
                "Restored run %s (%d results) from disk",
                restored.job_id, len(restored.results),
            )

    def get(self, job_id: str) -> JobStatus | None:
        return self._jobs.get(job_id)

    async def get_or_load(self, job_id: str) -> JobStatus | None:
        """In-memory first, then fall back to a persisted run."""
        job = self._jobs.get(job_id)
        if job is not None:
            return job
        if self.storage is None:
            return None
        restored = await self.storage.load_run(job_id)
        if restored is not None:
            self._add(restored)
        return restored

    async def _persist(self, job: JobStatus, *, force: bool = False) -> None:
        """Mirror in-flight job state to disk so any worker can serve a poll.

        `save_run` is an INSERT OR REPLACE, so repeat calls just overwrite the
        row. Errors are swallowed there -- persistence must never fail a run.
        """
        if self.storage is None:
            return
        now = time.monotonic()
        if not force and now - self._last_persist.get(job.job_id, 0.0) < PERSIST_THROTTLE_SECONDS:
            return
        self._last_persist[job.job_id] = now
        await self.storage.save_run(job, self._timeframes.get(job.job_id, ""))

    async def _finish(self, job: JobStatus, timeframe: str) -> None:
        """Mark a run as the latest and persist it."""
        self.last_completed = job
        self._timeframes[job.job_id] = timeframe
        await self._persist(job, force=True)

    def _add(self, job: JobStatus) -> None:
        self._jobs[job.job_id] = job
        while len(self._jobs) > MAX_JOBS:
            old_id, _ = self._jobs.popitem(last=False)
            task = self._tasks.pop(old_id, None)
            if task and not task.done():
                task.cancel()
            self._timeframes.pop(old_id, None)
            self._last_persist.pop(old_id, None)

    async def start(
        self,
        req: ScrapeRequest,
        ph_client: ProductHuntClient,
        provider: EnrichmentProvider | None,
        ahrefs: AhrefsClient | None = None,
        domain_age: DomainAgeClient | None = None,
    ) -> JobStatus:
        job = JobStatus(job_id=uuid.uuid4().hex[:12], state="queued")
        self._add(job)
        self._timeframes[job.job_id] = req.timeframe
        # Persist before returning the id: the first poll may land on another
        # worker before the task below has run at all.
        await self._persist(job, force=True)
        self._tasks[job.job_id] = asyncio.create_task(
            self._run(job, req, ph_client, provider, ahrefs, domain_age)
        )
        return job

    async def _run(
        self,
        job: JobStatus,
        req: ScrapeRequest,
        ph_client: ProductHuntClient,
        provider: EnrichmentProvider | None,
        ahrefs: AhrefsClient | None = None,
        domain_age: DomainAgeClient | None = None,
    ) -> None:
        try:
            rng = resolve_range(req.timeframe, date_from=req.date_from, date_to=req.date_to)
            job.range_from = rng.start.isoformat()
            job.range_to = rng.end.isoformat()
            job.state = "fetching"
            job.message = f"Fetching top {req.limit} from {label_for(req.timeframe, rng)}…"
            await self._persist(job, force=True)

            products = await ph_client.top_posts(rng, req.limit)
            # Stamped the moment Product Hunt answered, so the UI reports data
            # age rather than render time.
            job.fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            job.results = products
            job.total = len(products)

            if not products:
                job.state = "done"
                job.message = (
                    "No launches found in this window. Try a wider timeframe."
                )
                await self._finish(job, req.timeframe)
                return

            if not req.enrich or provider is None:
                for p in products:
                    p.enrichment_status = "no_email"
                    p.enrichment_note = "enrichment skipped"
                job.state = "done"
                job.message = f"Fetched {len(products)} launches (enrichment off)."
                await self._finish(job, req.timeframe)
                return

            # Product Hunt hides upvote counts for roughly the first four
            # hours of a launch day. While they're hidden every post reports
            # zero, so a votes-ordered list is provisional and won't line up
            # with the live site.
            if products and all(p.votes == 0 for p in products):
                job.notice = _hidden_votes_notice()

            job.state = "enriching"
            job.message = f"Fetched {len(products)} launches. Finding companies…"
            await self._persist(job, force=True)
            await self._enrich_all(job, products, provider, ahrefs, domain_age)
            await self._hydrate_leads(products)
            if ahrefs is not None and ahrefs.disabled_reason:
                job.notice = (job.notice + " " + ahrefs.disabled_reason).strip()
            apify_reason = getattr(provider, "disabled_reason", "")
            if apify_reason:
                job.notice = (job.notice + " " + apify_reason).strip()

            job.state = "done"
            found = sum(1 for p in products if p.email)
            job.message = f"Done — {found} of {len(products)} launches have an email."
            await self._finish(job, req.timeframe)

        except ProductHuntError as exc:
            job.state = "error"
            job.message = str(exc)
            log.warning("Job %s failed at Stage 1: %s", job.job_id, exc)
            await self._persist(job, force=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the UI
            job.state = "error"
            job.message = "Something went wrong on the server. Check the logs for details."
            log.exception("Job %s crashed: %s", job.job_id, exc)
            await self._persist(job, force=True)
        finally:
            for closeable in (provider, ahrefs, domain_age):
                if closeable is None:
                    continue
                try:
                    await closeable.aclose()
                except Exception:  # noqa: BLE001
                    log.debug("Close failed for %r", closeable, exc_info=True)

    async def _hydrate_leads(self, products: list[Product]) -> None:
        """Carry existing outreach state onto freshly fetched rows.

        This is the point of the leads table: a domain marked "Contacted" in
        yesterday's daily list must still read "Contacted" when it resurfaces
        in this week's, so two people don't double-contact the same company.
        """
        if self.storage is None:
            return
        domains = sorted({p.domain for p in products if p.domain})
        if not domains:
            return
        leads = await self.storage.leads_for(domains)
        for product in products:
            lead = leads.get(product.domain)
            if lead is None:
                continue
            product.lead_status = lead.status
            product.lead_note = lead.note
            product.lead_updated_by = lead.updated_by
            product.lead_updated_at = lead.updated_at
        log.info("Hydrated %d known leads onto %d products", len(leads), len(products))

    async def _enrich_all(
        self,
        job: JobStatus,
        products: list[Product],
        provider: EnrichmentProvider,
        ahrefs: AhrefsClient | None = None,
        domain_age: DomainAgeClient | None = None,
    ) -> None:
        """Enrich concurrently; the provider's own semaphore caps real traffic."""
        quota_hit = asyncio.Event()

        async def one(product: Product) -> None:
            if quota_hit.is_set():
                product.enrichment_status = "no_email"
                product.enrichment_note = "skipped — Serper quota exhausted"
                job.enriched += 1
                return

            product.enrichment_status = "running"

            cached = None
            if self.storage is not None and product.website_url:
                cached = await self.storage.cached_enrichment(product.website_url)
            if cached is not None and cached.get("email"):
                product.company_name = cached["company_name"]
                product.company_description = cached["company_description"]
                product.domain = cached["domain"]
                product.email = cached["email"]
                product.email_verified = bool(cached["email_verified"])
                product.email_source = cached["email_source"]
                note = cached["note"]
                product.enrichment_note = f"{note} (cached)" if note else "cached from a prior run"
                product.enrichment_status = "found" if product.email_verified else "unverified"
                if ahrefs is not None and product.domain:
                    product.domain_rating = await ahrefs.domain_rating(product.domain)
                if domain_age is not None and product.domain:
                    product.domain_age_years = await domain_age.domain_age_years(product.domain)
                job.enriched += 1
                await self._persist(job)
                return

            try:
                result = await provider.enrich(product)
            except SerperQuotaError as exc:
                quota_hit.set()
                job.message = str(exc)
                product.enrichment_status = "error"
                product.enrichment_note = str(exc)
            except SerperError as exc:
                product.enrichment_status = "error"
                product.enrichment_note = str(exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("Enrichment failed for %s: %s", product.product_name, exc)
                product.enrichment_status = "error"
                product.enrichment_note = "Lookup failed for this product."
            else:
                product.company_name = result.company_name
                product.company_description = result.company_description
                product.domain = result.domain
                product.email = result.email
                product.email_verified = result.email_verified
                product.email_source = result.email_source
                product.enrichment_note = result.note
                # DR only means something once a real domain is confirmed.
                if ahrefs is not None and result.domain:
                    product.domain_rating = await ahrefs.domain_rating(result.domain)
                if domain_age is not None and result.domain:
                    product.domain_age_years = await domain_age.domain_age_years(result.domain)
                if result.email and result.email_verified:
                    product.enrichment_status = "found"
                elif result.email:
                    product.enrichment_status = "unverified"
                else:
                    product.enrichment_status = "no_email"

                # Only a confirmed email is worth caching -- a miss stays
                # uncached so a later run still gets a fresh shot at it.
                if self.storage is not None and product.website_url and result.email:
                    await self.storage.upsert_cached_enrichment(
                        product.website_url,
                        {
                            "domain": result.domain,
                            "company_name": result.company_name,
                            "company_description": result.company_description,
                            "email": result.email,
                            "email_verified": result.email_verified,
                            "email_source": result.email_source,
                            "note": result.note,
                        },
                    )
            finally:
                job.enriched += 1
                # Throttled inside _persist, so this is cheap even at high
                # concurrency -- it keeps the progress bar recoverable.
                await self._persist(job)

        await asyncio.gather(*(one(p) for p in products))


registry = JobRegistry()
