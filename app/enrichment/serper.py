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
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app.enrichment.directcrawl import DirectCrawlClient
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

# Google often returns the Product Hunt launch page as the best result. Its
# snippet can contain the outbound "Company Info" domain even when the
# launch's real website is not indexed yet. Keep this deliberately URL-shaped
# so ordinary prose does not turn into a domain candidate.
_DOMAIN_RE = re.compile(
    r"(?<![@\w.-])(?:https?://)?(?:www\.)?"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,24}(?![\w.-])",
    re.IGNORECASE,
)


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


def _domains_in(text: str) -> list[str]:
    """Return unique, company-looking domains embedded in search text."""
    found: list[str] = []
    for raw in _DOMAIN_RE.findall(text or ""):
        domain = normalize_domain(raw)
        if domain and is_company_host(domain) and domain not in found:
            found.append(domain)
    return found


def _domain_from_search_results(
    results: list[dict[str, Any]], product_name: str,
    *, allow_named_result: bool = False,
) -> str:
    """Resolve from ordinary result links, then trusted PH snippets."""
    # Prefer an actual result link whose hostname matches the product. This
    # keeps the existing namesake protection for generic web results.
    for result in results[:8]:
        link = result.get("link") or ""
        if is_company_host(link):
            candidate = normalize_domain(link)
            if domain_matches_product(candidate, product_name):
                return candidate

            # Explicit "official website" searches can legitimately return
            # a hosted/subdomain URL whose hostname differs from the launch
            # name (e.g. an app deployed on Netlify). Require the result title
            # itself to identify the product before trusting that relaxed
            # candidate.
            if allow_named_result and _result_names_product(result, product_name):
                return candidate

    return _domain_from_producthunt_results(results)


def _result_names_product(result: dict[str, Any], product_name: str) -> bool:
    """Whether a search result title clearly identifies the product."""
    title = re.sub(r"[^a-z0-9]", "", str(result.get("title") or "").lower())
    name = re.sub(r"[^a-z0-9]", "", (product_name or "").lower())
    return bool(title and name and (title == name or name in title))


def _domain_from_producthunt_results(results: list[dict[str, Any]]) -> str:
    """Read an official domain exposed in a Product Hunt result snippet.

    Product Hunt's indexed page commonly includes text such as ``Company
    Info pageindex.ai`` even though its API only gives us a Cloudflare
    ``/r/...`` redirect. Only Product Hunt result pages get this relaxed
    trust; unrelated search snippets still require a name/domain match.
    """
    for result in results[:10]:
        source = normalize_domain(str(result.get("link") or ""))
        if not (source == "producthunt.com" or source.endswith(".producthunt.com")):
            continue
        text = " ".join(
            str(result.get(key) or "") for key in ("title", "snippet")
        )
        candidates = _domains_in(text)
        if not candidates:
            continue
        # Only accept a domain when the snippet explicitly identifies it as
        # the company's website. This prevents a random domain mentioned in
        # a launch comment from becoming the lead's domain.
        lowered = text.lower()
        labelled = any(
            marker in lowered
            for marker in ("company info", "visit website", "official website")
        )
        if labelled:
            return candidates[0]
    return ""


class SerperProvider:
    """Serper-backed implementation of the EnrichmentProvider protocol."""

    name = "serper"

    def __init__(
        self,
        api_key: str | None,
        concurrency: int = 3,
        delay_seconds: float = 0.35,
        timeout: float = 25.0,
        directcrawl: DirectCrawlClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._delay = delay_seconds
        self._timeout = timeout
        self._directcrawl = directcrawl
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
        if self._directcrawl is not None:
            await self._directcrawl.aclose()

    async def enrich(self, product: Product) -> Enrichment:
        ok, reason = self.available()
        if not ok:
            raise SerperError(reason)
        if self._quota_exhausted:
            raise SerperQuotaError(
                "Serper quota exhausted -- remaining products were left un-enriched."
            )

        cache_key = f"name:{product.product_name.strip().lower()}"
        # A domain resolved upstream (e.g. PHPageResolver, straight from
        # Product Hunt's own page) is trusted directly -- no need to spend a
        # search-based discovery call re-guessing something already known.
        domain = product.domain or await self._discover_domain(product)
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
        email_source = ""
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
                email_source = "serper-snippet"
                note_parts.append(f"email via {label}")
                break

        # Google's snippets are the weakest email source -- they never
        # reflect a page's mailto: links or Cloudflare-obfuscated markup.
        # One more free pass over the site itself, once a domain is known.
        if not email and domain and self._directcrawl is not None:
            crawled_email, crawled_verified, crawled_source = await self._directcrawl.find_email(
                domain, product.product_name
            )
            if crawled_email:
                email = crawled_email
                email_source = crawled_source
                note_parts.append(f"email via {crawled_source}")

        # An email is "verified" only when it sits on the domain we resolved
        # for this product. Name-matched addresses found without a confirmed
        # domain are still returned, but flagged so the UI can say so.
        verified = bool(email and domain) and registrable_domain(
            email.rsplit("@", 1)[-1]
        ) == registrable_domain(domain)
        if email_source == "guessed":
            verified = False
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
            email_source=email_source,
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

        domain = _domain_from_search_results(organic, product.product_name)

        # New launches are frequently not indexed under their own domain yet,
        # while Product Hunt's page is indexed immediately. Ask specifically
        # for that page and read its Company Info/Visit website snippet. This
        # is also the safe place to accept a domain whose name differs from
        # the product name (for example a product launched by its parent
        # company), because the source itself is Product Hunt.
        if not domain and product.producthunt_url:
            parsed = urlparse(product.producthunt_url)
            path = parsed.path.strip("/")
            if path:
                slug = path.rsplit("/", 1)[-1]
                ph_query = (
                    f'site:producthunt.com "{slug}" "Company Info"'
                )
                ph_results = await self._search(ph_query)
                domain = _domain_from_producthunt_results(ph_results)

        # A launch can be too new for Product Hunt's page to carry a useful
        # search snippet. The official-website wording is a reliable second
        # discovery pass and handles products whose tagline is not indexed.
        if not domain:
            if "." in product.product_name:
                # Serper free accounts reject quoted bare-domain patterns.
                official_query = f"{product.product_name} official website"
            else:
                official_query = f'"{product.product_name}" official website'
            official_results = await self._search(official_query)
            domain = _domain_from_search_results(
                official_results, product.product_name, allow_named_result=True
            )

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
