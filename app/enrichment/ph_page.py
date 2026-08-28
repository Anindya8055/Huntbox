"""Resolve a product's real domain straight from its own Product Hunt page
(Stage 2, free, no search index involved).

``product.website_url`` is the Cloudflare-protected ``producthunt.com/r/...``
redirect (see :mod:`app.enrichment.apify`) -- a real browser is needed to
follow it. ``product.producthunt_url`` is a completely different, ordinary
page (``producthunt.com/products/{slug}``): server-rendered, publicly
crawlable, and it has to be, for Product Hunt's own SEO. It carries the real
external "Visit website" link the instant a product goes live.

Search-based domain discovery (:mod:`app.enrichment.serper`) -- including
reading a domain out of *this same page's* Google-cached snippet -- still
depends on Google having indexed something first. For a same-day launch
that lag is exactly the gap this closes: fetch the page directly, no index,
no crawl budget, no paid actor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx
from bs4 import BeautifulSoup

from app.enrichment.emails import is_company_host, normalize_domain
from app.models import Product

log = logging.getLogger("huntbox.ph_page")

# Hosts Product Hunt's own page legitimately links to that are never the
# product's own site -- its CDN/asset hosts and its own domain variants.
_PH_ASSET_HOSTS = {"ph-files.imgix.net", "producthunt.com", "www.producthunt.com"}

_IMAGE_EXT_RE = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|ico|mp4|webm)(?:[?#]|$)", re.IGNORECASE)


def _looks_like_asset(url: str) -> bool:
    return bool(_IMAGE_EXT_RE.search(url))


class PHPageResolver:
    """Free, in-house resolver: fetch a product's own PH page, read its
    "Visit website" link. Same soft-fail/cached/semaphored shape as the
    other Stage 2 clients -- never raises, always returns "" on failure."""

    name = "ph_page"

    def __init__(self, concurrency: int = 3, timeout: float = 8.0) -> None:
        self._timeout = timeout
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, str] = {}

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def resolve_domain(self, product: Product) -> str:
        url = product.producthunt_url
        if not url:
            return ""
        if url in self._cache:
            return self._cache[url]

        html = await self._fetch(url)
        domain = _extract_domain(html) if html else ""
        self._cache[url] = domain
        return domain

    async def _fetch(self, url: str) -> str:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; HuntboxBot/1.0)"},
            )
        async with self._sem:
            try:
                resp = await self._client.get(url)
            except httpx.HTTPError as exc:
                log.info("Could not fetch PH page %s: %s", url, exc)
                return ""
        if resp.status_code != 200:
            return ""
        return resp.text


def _extract_domain(html: str) -> str:
    """"Visit website" anchor first, then the page's own __NEXT_DATA__ blob."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # noqa: BLE001 - a malformed page must never break the run
        return ""

    domain = _from_visit_website_anchor(soup)
    if domain:
        return domain
    return _from_next_data(soup)


def _from_visit_website_anchor(soup: BeautifulSoup) -> str:
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not is_company_host(href) or _looks_like_asset(href):
            continue
        text = a.get_text(" ", strip=True).lower()
        rel = " ".join(a.get("rel") or []).lower()
        if "visit" in text or "nofollow" in rel:
            candidate = normalize_domain(href)
            if candidate:
                return candidate
    return ""


def _from_next_data(soup: BeautifulSoup) -> str:
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return ""
    try:
        data = json.loads(tag.string)
    except ValueError:
        return ""

    for raw in _urls_in(data):
        if _looks_like_asset(raw):
            continue
        candidate = normalize_domain(raw)
        if candidate and candidate not in _PH_ASSET_HOSTS and is_company_host(raw):
            return candidate
    return ""


def _urls_in(node: object) -> list[str]:
    """Walk a parsed JSON tree, collecting every http(s) string value."""
    found: list[str] = []
    if isinstance(node, dict):
        for value in node.values():
            found.extend(_urls_in(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_urls_in(item))
    elif isinstance(node, str) and node.startswith(("http://", "https://")):
        found.append(node)
    return found
