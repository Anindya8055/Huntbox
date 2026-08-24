"""Domain age lookup via RDAP.

Old-but-weak-DR domains are prime outreach targets -- an established
company that never invested in SEO. RDAP (RFC 7482), not legacy WHOIS text
scraping, is used here: it's structured JSON, needs no API key, and doesn't
require per-registrar text parsing.

    GET https://rdap.org/domain/{domain}
    -> {"events": [{"eventAction": "registration", "eventDate": "..."}]}

``rdap.org`` is IANA's free public RDAP bootstrap redirector -- it resolves
the query to whichever registry actually holds the domain.

Coverage caveat: RDAP is solid for gTLDs (.com/.net/.org/.io/...) but
inconsistent for some ccTLDs, and GDPR-redacted registrations omit the
registration date entirely. Expect a non-trivial ``None`` rate -- treat this
as a helpful signal, not an authoritative record.

Failures here are always soft: age is a nice-to-have column, never a reason
to fail a hunt.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

log = logging.getLogger("huntbox.domain_age")

RDAP_URL = "https://rdap.org/domain/{domain}"


class DomainAgeClient:
    def __init__(self, concurrency: int = 2, timeout: float = 15.0) -> None:
        self._timeout = timeout
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, float | None] = {}
        self._disabled = False
        self._disabled_reason = ""

    @property
    def disabled_reason(self) -> str:
        return self._disabled_reason

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def domain_age_years(self, domain: str) -> float | None:
        """Years since registration, or None if unavailable. Never raises."""
        if not domain or self._disabled:
            return None
        if domain in self._cache:
            return self._cache[domain]

        if self._client is None:
            # rdap.org is a bootstrap redirector -- it 302s to whichever
            # registry actually holds the domain, so redirects must be followed.
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=True)

        async with self._sem:
            try:
                resp = await self._client.get(
                    RDAP_URL.format(domain=domain),
                    headers={"Accept": "application/rdap+json"},
                )
            except httpx.HTTPError as exc:
                log.info("RDAP request failed for %s: %s", domain, exc)
                self._cache[domain] = None
                return None

        if resp.status_code == 429:
            self._disabled = True
            self._disabled_reason = "RDAP rate limit reached. Domain age is off for the rest of this run."
            log.warning(self._disabled_reason)
            return None
        if resp.status_code != 200:
            # 404s are routine (unregistered/unsupported TLDs) -- not worth logging loudly.
            log.debug("RDAP HTTP %d for %s", resp.status_code, domain)
            self._cache[domain] = None
            return None

        try:
            body = resp.json()
            events = body.get("events") or []
            registered = next(
                (e.get("eventDate") for e in events if e.get("eventAction") == "registration"),
                None,
            )
            age = _years_since(registered) if registered else None
        except (ValueError, AttributeError, TypeError):
            log.warning("Unexpected RDAP payload for %s", domain)
            age = None

        self._cache[domain] = age
        return age


def _years_since(iso_date: str) -> float | None:
    try:
        registered = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    except ValueError:
        return None
    if registered.tzinfo is None:
        registered = registered.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - registered).days
    return round(max(0, days) / 365.25, 1)
