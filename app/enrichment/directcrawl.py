"""Direct-site crawl for emails Apify/Serper missed (Stage 2, free fallback).

Both existing providers have a structural blind spot: Serper only ever reads
Google's cached snippet text (a few hundred chars), and Apify's crawl budget
(``maxRequestsPerStartUrl``) is spent on whatever pages its own link-following
heuristics pick, with no guarantee it reaches ``/contact`` on a larger site.
Neither extracts ``mailto:`` links or decodes Cloudflare's email-obfuscation
markup, both well-documented, completely free techniques.

This client is the "one more free pass" that runs once a domain is already
known but no email was found: it fetches the homepage plus a fixed list of
paths that almost always carry a contact address, extracts every ``mailto:``
link, decodes Cloudflare-protected addresses, and regex-scans the visible
text (including light "name [at] domain [dot] com" deobfuscation) via the
existing :mod:`app.enrichment.emails` filtering/scoring -- no duplicate logic.

An opt-in (``guess_fallback=True``, off by default) last resort can
synthesize a role-address guess (``hello@domain`` etc.) when nothing on the
site itself yields an email, gated on the domain having mail servers (a free
DNS-over-HTTPS MX lookup). It stays off by default: an MX check alone can't
tell a real small company's inbox from a large platform's shared corporate
domain, so a plausible-but-wrong guess is worse for an outreach tool than no
result at all. When enabled, a guess is always flagged
``email_verified=False`` and labelled "guessed" so it's never confused with
a confirmed find.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx
from bs4 import BeautifulSoup

from app.enrichment.emails import best_email, extract_emails

log = logging.getLogger("huntbox.directcrawl")

# Paths that almost always carry a contact address, tried alongside the
# homepage instead of hoping a generic crawl budget reaches them.
COMMON_CONTACT_PATHS = (
    "/",
    "/contact",
    "/contact-us",
    "/about",
    "/about-us",
    "/team",
    "/support",
    "/privacy",
    "/privacy-policy",
    "/legal",
    "/impressum",
)

# Ordered best-first, mirrors emails._ROLE_PREFERENCE -- used only for the
# last-resort guess, never returned as a verified find.
_GUESS_LOCALPARTS = ("hello", "contact", "info", "support")

# "name [at] domain [dot] com" / "name (at) domain (dot) com" style
# obfuscation -- cheap to undo before handing text to the real email regex.
_AT_DOT_RE = re.compile(
    r"\s*[\[(]\s*at\s*[\])]\s*|\s+at\s+",
    re.IGNORECASE,
)
_DOT_RE = re.compile(
    r"\s*[\[(]\s*dot\s*[\])]\s*|\s+dot\s+",
    re.IGNORECASE,
)

DNS_URL = "https://dns.google/resolve"


def _deobfuscate(text: str) -> str:
    """Undo the common 'name [at] domain [dot] com' text obfuscation."""
    text = _AT_DOT_RE.sub("@", text)
    text = _DOT_RE.sub(".", text)
    return text


def decode_cf_email(hex_str: str) -> str:
    """Reverse Cloudflare's single-byte-XOR email obfuscation.

    The first hex byte is the XOR key; every following byte is XORed with it
    to recover the address. Documented, reversible-by-design obfuscation,
    not encryption -- see e.g. https://usamaejaz.com/cloudflare-email-decoding
    """
    try:
        data = bytes.fromhex(hex_str.strip())
    except ValueError:
        return ""
    if len(data) < 2:
        return ""
    key = data[0]
    decoded = bytes(b ^ key for b in data[1:])
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _candidates_from_html(html: str) -> list[str]:
    """Every plausible email string reachable from one page's HTML."""
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().startswith("mailto:"):
            addr = href[len("mailto:"):].split("?", 1)[0].strip()
            if addr:
                found.append(addr)

    for el in soup.select("[data-cfemail]"):
        decoded = decode_cf_email(el["data-cfemail"])
        if decoded:
            found.append(decoded)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/cdn-cgi/l/email-protection#" in href:
            decoded = decode_cf_email(href.rsplit("#", 1)[-1])
            if decoded:
                found.append(decoded)

    text = soup.get_text(" ", strip=True)
    found.extend(extract_emails(text))
    found.extend(extract_emails(_deobfuscate(text)))
    return found


class DirectCrawlClient:
    """Free, in-house fallback: fetch known-likely pages, extract, guess."""

    name = "directcrawl"

    def __init__(
        self,
        concurrency: int = 3,
        timeout: float = 8.0,
        # An MX check alone can't tell a real small company's inbox from a
        # large platform's shared corporate domain (labs.google,
        # gemini.google, ...) -- a wrong-but-plausible guess is worse than
        # an empty cell for an outreach tool, so this stays opt-in.
        guess_fallback: bool = False,
    ) -> None:
        self._timeout = timeout
        self._guess_fallback = guess_fallback
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[str, bool, str]] = {}

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def find_email(self, domain: str, product_name: str = "") -> tuple[str, bool, str]:
        """Best-effort (email, verified, source_label) for a known domain.

        Never raises -- every failure just means an empty result. ``source``
        is one of "directcrawl" (found on the site), "guessed" (MX-checked
        role-address synthesis), or "" (nothing at all).
        """
        if not domain:
            return "", False, ""
        if domain in self._cache:
            return self._cache[domain]

        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; HuntboxBot/1.0)"},
            )

        candidates: list[str] = []
        for scheme in ("https", "http"):
            candidates = await self._crawl(f"{scheme}://{domain}")
            if candidates:
                break

        email = best_email(candidates, domain, product_name=product_name)
        if email:
            result = (email, True, "directcrawl")
            self._cache[domain] = result
            return result

        if self._guess_fallback and await self._has_mx(domain):
            guess = f"{_GUESS_LOCALPARTS[0]}@{domain}"
            result = (guess, False, "guessed")
            self._cache[domain] = result
            return result

        result = ("", False, "")
        self._cache[domain] = result
        return result

    async def _crawl(self, base_url: str) -> list[str]:
        pages = await asyncio.gather(
            *(self._fetch(base_url.rstrip("/") + path) for path in COMMON_CONTACT_PATHS),
            return_exceptions=True,
        )
        candidates: list[str] = []
        for page in pages:
            if isinstance(page, str) and page:
                try:
                    candidates.extend(_candidates_from_html(page))
                except Exception:  # noqa: BLE001 - a malformed page must never break the run
                    log.debug("Failed to parse page for %s", base_url, exc_info=True)
        return candidates

    async def _fetch(self, url: str) -> str:
        async with self._sem:
            try:
                resp = await self._client.get(url)
            except httpx.HTTPError:
                return ""
        if resp.status_code != 200:
            return ""
        content_type = resp.headers.get("content-type", "")
        if content_type and "html" not in content_type:
            return ""
        return resp.text

    async def _has_mx(self, domain: str) -> bool:
        """Free DNS-over-HTTPS MX check -- confirms mail is even possible
        before offering a guessed address, without needing a DNS library."""
        try:
            async with self._sem:
                resp = await self._client.get(
                    DNS_URL, params={"name": domain, "type": "MX"}, timeout=6.0
                )
            if resp.status_code != 200:
                return False
            body = resp.json()
            return bool(body.get("Answer"))
        except Exception:  # noqa: BLE001 - MX check is advisory, never fatal
            return False
