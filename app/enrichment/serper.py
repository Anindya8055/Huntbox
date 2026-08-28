"""Serper.dev enrichment provider (Stage 2).

Product Hunt masks every outbound link behind a Cloudflare-protected
redirect, so the real company domain is unknown when a product arrives
here. The provider therefore runs a discovery search first, then the three
email strategies against whatever domain that turned up:

  0. "{name}" {tagline}                 -> discover the company domain
  1. site:{domain} email                -> emails on the company's own site
  2. "{name}" email contact             -> publicly listed contact addresses
  3. "{domain}" "@{domain}"             -> an address at that exact domain

Strategies stop at the first usable email. Results are cached per domain
(and per product name, for discovery) for the process lifetime, and calls
are gated by a semaphore plus a small inter-call delay.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.enrichment.emails import (
    best_email,
    domain_matches_product,
    extract_emails,
    is_company_host,
    normalize_domain,
    registrable_domain,
)
from app.models import Enrichment, Product

log = logging.getLogger("huntbox.serper")

SEARCH_URL = "https://google.serper.dev/search"


class SerperError(RuntimeError):
    """A user-presentable failure talking to Serper."""


class SerperQuotaError(SerperError):
    """Quota exhausted or rate limited -- the run should stop calling out."""


def _snippet_blob(results: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for r in results:
        parts.extend(
            str(r.get(k, "")) for k in ("title", "snippet", "link") if r.get(k)
        )
    return "\n".join(parts)


class SerperProvider:
    """Serper-backed implementation of the EnrichmentProvider protocol."""

    name = "serper"

    def __init__(
        self,
        api_key: str | None,
        concurrency: int = 3,
        delay_seconds: float = 0.35,
        timeout: float = 25.0,
    ) -> None:
        self._api_key = api_key
        self._delay = delay_seconds
        self._timeout = timeout
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, Enrichment] = {}
        self._domain_cache: dict[str, str] = {}
        self._quota_exhausted = False

    # -- protocol ---------------------------------------------------------

    def available(self) -> tuple[bool, str]:
        if not self._api_key:
            return False, (
                "SERPER_API_KEY is missing. Add it to your .env file to enable "
                "company and email enrichment."
            )
        return True, ""

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def enrich(self, product: Product) -> Enrichment:
        ok, reason = self.available()
        if not ok:
            raise SerperError(reason)
        if self._quota_exhausted:
            raise SerperQuotaError(
                "Serper quota exhausted -- remaining products were left un-enriched."
            )

        cache_key = f"name:{product.product_name.strip().lower()}"
        domain = await self._discover_domain(product)
        if domain:
            cache_key = f"domain:{domain}"

        cached = self._cache.get(cache_key)
        if cached is not None:
            log.debug("Cache hit for %s", cache_key)
            return cached

        result = await self._run_strategies(product, domain)
        self._cache[cache_key] = result
        return result

    # -- internals --------------------------------------------------------

    async def _run_strategies(self, product: Product, domain: str) -> Enrichment:
        company_name = _company_name_from(product, domain)
        description = ""
        note_parts: list[str] = []

        queries: list[tuple[str, str]] = []
        if domain:
            queries.append(("site-email", f"site:{domain} email"))
        queries.append(("name-contact", f'"{product.product_name}" email contact'))
        if domain:
            # Serper's free tier rejects a query that is nothing but a quoted
            # domain, so the bare `"{domain}" "@{domain}"` form 400s. Adding
            # a plain term keeps the same intent and is accepted.
            queries.append(("domain-at", f'{domain} contact "@{domain}"'))

        email = ""
        for label, query in queries:
            organic = await self._search(query)
            if not organic:
                continue
            if not description:
                description = (organic[0].get("snippet") or "").strip()
            candidates = extract_emails(_snippet_blob(organic))
            picked = best_email(candidates, domain, product_name=product.product_name)
            if picked:
                email = picked
                note_parts.append(f"email via {label}")
                break

        # An email is "verified" only when it sits on the domain we resolved
        # for this product. Name-matched addresses found without a confirmed
        # domain are still returned, but flagged so the UI can say so.
        verified = bool(email and domain) and registrable_domain(
            email.rsplit("@", 1)[-1]
        ) == registrable_domain(domain)
        if email and not verified:
            note_parts.append("unverified: no confirmed company domain")
        if not email:
            note_parts.append(
                "no public email found"
                if domain
                else "no company domain resolved; no verified email"
            )

        return Enrichment(
            company_name=company_name,
            company_description=_pick_description(product, description),
            domain=domain,
            email=email,
            email_verified=verified,
            note="; ".join(note_parts),
        )

    async def _discover_domain(self, product: Product) -> str:
        """Strategy 0: find the real company domain PH hides behind /r/ links."""
        key = product.product_name.strip().lower()
        if key in self._domain_cache:
            return self._domain_cache[key]

        query = f'"{product.product_name}" {product.tagline}'.strip()
        organic = await self._search(query)
        if not organic and "." in product.product_name:
            # Free-tier Serper rejects a quoted term that already looks like
            # a bare domain (e.g. "akta.pro") as a disallowed query pattern.
            # Dropping the quotes keeps the same intent without tripping it.
            organic = await self._search(f"{product.product_name} {product.tagline}".strip())
        domain = ""
        for result in organic[:8]:
            link = result.get("link") or ""
            if not is_company_host(link):
                continue
            candidate = normalize_domain(link)
            # A top result is only trusted when the domain actually looks
            # like this product -- otherwise we adopt a louder namesake.
            if domain_matches_product(candidate, product.product_name):
                domain = candidate
                break

        self._domain_cache[key] = domain
        if domain:
            log.info("Resolved %s -> %s", product.product_name, domain)
        else:
            log.info("No domain found for %s", product.product_name)
        return domain

    async def _search(self, query: str) -> list[dict[str, Any]]:
        if self._quota_exhausted:
            return []
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)

        async with self._sem:
            try:
                resp = await self._client.post(
                    SEARCH_URL,
                    headers={
                        "X-API-KEY": self._api_key or "",
                        "Content-Type": "application/json",
                    },
                    json={"q": query, "num": 10},
                )
            except httpx.TimeoutException:
                log.warning("Serper timed out for query: %s", query)
                return []
            except httpx.HTTPError as exc:
                log.warning("Serper transport error for %r: %s", query, exc)
                return []
            finally:
                if self._delay:
                    await asyncio.sleep(self._delay)

        if resp.status_code in (401, 403):
            self._quota_exhausted = True
            raise SerperQuotaError(
                "Serper rejected the API key (HTTP %d). Check SERPER_API_KEY."
                % resp.status_code
            )
        if resp.status_code == 429:
            self._quota_exhausted = True
            raise SerperQuotaError(
                "Serper rate limit or quota reached. Remaining products were left un-enriched."
            )
        if resp.status_code == 400:
            # Usually "query pattern not allowed for free accounts" -- skip
            # this strategy rather than failing the whole product.
            log.warning("Serper rejected query %r: %s", query, resp.text[:120])
            return []
        if resp.status_code != 200:
            log.warning("Serper HTTP %d for query %r", resp.status_code, query)
            return []

        try:
            body = resp.json()
        except ValueError:
            log.warning("Serper returned malformed JSON for %r", query)
            return []

        organic = body.get("organic")
        return organic if isinstance(organic, list) else []


def _company_name_from(product: Product, domain: str) -> str:
    """Best-effort company name: the registrable domain's label, else the product."""
    reg = registrable_domain(domain)
    if reg:
        label = reg.split(".")[0]
        if label and label.lower() != product.product_name.strip().lower():
            return label if len(label) > 3 else product.product_name
    return product.product_name


def _pick_description(product: Product, snippet: str) -> str:
    """Use the search snippet only when Product Hunt's own description is thin."""
    own = (product.description or "").strip()
    if len(own) >= 80:
        return own
    return snippet or own
