"""Apify Contact Info Scraper enrichment provider (Stage 2, primary).

Product Hunt's own ``website`` field is a Cloudflare-protected
``producthunt.com/r/...`` redirect, so :mod:`app.enrichment.serper` never
touches it and resolves the real domain purely through search. This
provider instead points Apify's ``vdrmota/contact-info-scraper`` actor
directly at that redirect: the actor runs a real browser/proxy, so it can
follow the Cloudflare hop to the actual site, then crawls it (home, about,
contact pages) for emails/phones/socials -- coverage Google snippets can't
match.

Docs / actor: https://apify.com/vdrmota/contact-info-scraper

If Apify has no ``website_url`` to work with, times out, or comes back
empty, this provider falls through to a wrapped :class:`SerperProvider`
instance so a run never loses coverage it already had -- Apify is additive,
never a regression.

NOTE: the actor's exact input schema (``startUrls`` field name, crawl-depth
knobs) should be checked in the Apify console against the live actor before
relying on this in production; the body below matches the actor's
documented "Input" tab as of the plan that shipped this file, but Apify
actors evolve their schemas independently of this codebase.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.enrichment.directcrawl import DirectCrawlClient
from app.enrichment.emails import (
    best_email,
    is_company_host,
    normalize_domain,
    registrable_domain,
)
from app.enrichment.serper import SerperProvider
from app.models import Enrichment, Product

log = logging.getLogger("huntbox.apify")

RUN_URL = "https://api.apify.com/v2/acts/vdrmota~contact-info-scraper/run-sync-get-dataset-items"


class ApifyContactProvider:
    """Apify-backed implementation of the EnrichmentProvider protocol.

    Wraps a :class:`SerperProvider` as an automatic fallback -- composition,
    not inheritance, so Serper's own lifecycle (client, cache, quota state)
    stays independent and is closed exactly once via :meth:`aclose`.
    """

    name = "apify"

    def __init__(
        self,
        api_token: str | None,
        serper_fallback: SerperProvider,
        concurrency: int = 2,
        # run-sync-get-dataset-items blocks until the actor finishes; pulled
        # the last 10 real runs from the Apify API and durations ranged from
        # 8s to 98s, so 45s was aborting genuinely-successful crawls (paying
        # for the run, then getting nothing and falling through to Serper
        # anyway). 100s covers the observed range with headroom.
        timeout: float = 100.0,
        directcrawl: DirectCrawlClient | None = None,
    ) -> None:
        self._api_token = api_token
        self._serper = serper_fallback
        self._timeout = timeout
        self._directcrawl = directcrawl
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, Enrichment] = {}
        # Set once Apify tells us the account can't run the actor (out of
        # usage credit, or a plan that blocks it). After that every further
        # call would just pay for a doomed HTTP round-trip before falling
        # back anyway, so we short-circuit straight to Serper instead.
        self.disabled_reason = ""

    # -- protocol ---------------------------------------------------------

    def available(self) -> tuple[bool, str]:
        if not self._api_token:
            return False, (
                "APIFY_API_TOKEN is missing. Add it to your .env file to crawl "
                "each product's real site for contact details."
            )
        return True, ""

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        await self._serper.aclose()

    async def enrich(self, product: Product) -> Enrichment:
        ok, _ = self.available()
        if not ok or self.disabled_reason or not product.website_url:
            return await self._serper.enrich(product)

        cache_key = product.website_url
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        result = await self._crawl(product)
        if result is None or not result.domain:
            log.info(
                "Apify found nothing for %s, falling back to Serper",
                product.product_name,
            )
            result = await self._serper.enrich(product)
            if result.domain or result.email:
                note = "; ".join(p for p in (result.note, "resolved via serper fallback") if p)
                result = result.model_copy(update={"note": note})
        else:
            if not result.email and self._directcrawl is not None:
                result = await self._try_directcrawl(result, product)
            self._cache[cache_key] = result
        return result

    async def _try_directcrawl(self, result: Enrichment, product: Product) -> Enrichment:
        """Apify found a domain but no email -- one more free pass over the
        site's own likely contact pages before accepting an empty result."""
        email, verified, source = await self._directcrawl.find_email(
            result.domain, product.product_name
        )
        if not email:
            return result
        note = "; ".join(p for p in (result.note, f"email via {source}") if p)
        return result.model_copy(
            update={
                "email": email,
                "email_verified": verified,
                "email_source": source,
                "note": note,
            }
        )

    # -- internals --------------------------------------------------------

    async def _crawl(self, product: Product) -> Enrichment | None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)

        payload = {
            "startUrls": [{"url": product.website_url}],
            "maxRequestsPerStartUrl": 15,
            "sameDomain": True,
        }

        async with self._sem:
            resp = await self._post_with_retry(payload, product.product_name)
        if resp is None:
            return None

        if resp.status_code == 402:
            # 402 covers more than one account state -- only the credit/
            # payment-exhaustion type is run-wide and permanent; anything
            # else (e.g. a transient per-actor-run cap) might clear up on
            # the very next product, so only that specific type should
            # give up on Apify for the rest of this run.
            error_type = ""
            try:
                error_type = str((resp.json() or {}).get("error", {}).get("type") or "")
            except ValueError:
                pass
            if "usage" in error_type or "payment" in error_type:
                self.disabled_reason = (
                    "Apify has no usage credit left this billing period, so "
                    "every product fell back to Serper (weaker domain/email "
                    "coverage). Add credit at console.apify.com/billing to "
                    "restore full crawling."
                )
                log.warning("Apify out of usage credit (%s) -- disabling for rest of run", error_type)
            else:
                log.warning("Apify HTTP 402 (%s) for %s", error_type or "unknown", product.product_name)
            return None

        if resp.status_code not in (200, 201):
            # run-sync-get-dataset-items returns 201 Created on a normal
            # successful run, not 200 -- only treat other codes as failure.
            log.warning(
                "Apify HTTP %d for %s: %s",
                resp.status_code,
                product.product_name,
                resp.text[:160],
            )
            return None

        try:
            items = resp.json()
        except ValueError:
            log.warning("Apify returned malformed JSON for %s", product.product_name)
            return None
        if not isinstance(items, list) or not items:
            return None

        return _items_to_enrichment(items, product)

    async def _post_with_retry(self, payload: dict, product_name: str) -> httpx.Response | None:
        """One retry on a transient failure -- real run durations span
        8-98s (see __init__), so a single timeout is plausibly transient,
        and a wasted actor run costs the same whether or not we retry."""
        for attempt in (1, 2):
            try:
                return await self._client.post(
                    RUN_URL, params={"token": self._api_token}, json=payload
                )
            except httpx.TimeoutException:
                if attempt == 2:
                    log.warning("Apify timed out twice for %s", product_name)
                    return None
                log.info("Apify timed out for %s, retrying once", product_name)
            except httpx.HTTPError as exc:
                log.warning("Apify transport error for %s: %s", product_name, exc)
                return None
        return None


def _items_to_enrichment(items: list[dict[str, Any]], product: Product) -> Enrichment | None:
    domain = ""
    all_emails: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue

        if not domain:
            # The actor's own "domain" field reports the *original* start
            # URL's host, not where the crawl actually landed -- so a PH
            # redirect link comes back as "producthunt.com" even when
            # scrapedUrls show it correctly followed through to the real
            # site. scrapedUrls (and "url", if present) reflect the actual
            # crawled pages and are trustworthy; the bare "domain" field is
            # only a last resort, and never when it's Product Hunt itself.
            candidates: list[str] = []
            for key in ("url", "scrapedUrls"):
                val = item.get(key)
                if isinstance(val, str):
                    candidates.append(val)
                elif isinstance(val, list):
                    candidates.extend(str(v) for v in val if v)
            candidates.append(str(item.get("domain") or ""))

            for raw in candidates:
                candidate = normalize_domain(raw)
                if candidate and is_company_host(candidate):
                    domain = candidate
                    break

        emails = item.get("emails")
        if isinstance(emails, list):
            all_emails.extend(str(e) for e in emails if e)

    if not domain:
        return None

    email = best_email(all_emails, domain, product_name=product.product_name)
    verified = bool(email) and registrable_domain(email.rsplit("@", 1)[-1]) == registrable_domain(domain)
    reg = registrable_domain(domain)
    label = reg.split(".")[0] if reg else ""
    company_name = label if len(label) > 3 else product.product_name

    note = "domain+email via apify" if email else "domain via apify; no email found on site"
    return Enrichment(
        company_name=company_name,
        company_description=(product.description or "").strip(),
        domain=domain,
        email=email,
        email_verified=verified,
        email_source="apify" if email else "",
        note=note,
    )
