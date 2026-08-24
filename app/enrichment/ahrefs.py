"""Ahrefs Domain Rating lookup.

Uses the free public endpoint:
    GET https://api.ahrefs.com/v3/public/domain-rating-free?target={domain}
    Authorization: Bearer <token>
    -> {"domain_rating": {"domain_rating": 72.0, "license": "..."}}

DR is a 0-100 logarithmic measure of a domain's backlink profile strength, so
it only means anything once Stage 2 has resolved a real domain. Products with
no confirmed domain simply have no DR.

Failures here are always soft: DR is a nice-to-have column, never a reason to
fail a hunt.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger("huntbox.ahrefs")

API_URL = "https://api.ahrefs.com/v3/public/domain-rating-free"


class AhrefsClient:
    def __init__(
        self,
        api_key: str | None,
        concurrency: int = 2,
        timeout: float = 20.0,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, float | None] = {}
        self._disabled = False
        self._disabled_reason = ""

    def available(self) -> tuple[bool, str]:
        if not self._api_key:
            return False, (
                "AHREFS_API_KEY is missing. Add it to your .env file to show "
                "Domain Rating next to each website."
            )
        return True, ""

    @property
    def disabled_reason(self) -> str:
        return self._disabled_reason

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def domain_rating(self, domain: str) -> float | None:
        """DR for a domain, or None if unavailable. Never raises."""
        if not domain or self._disabled or not self._api_key:
            return None
        if domain in self._cache:
            return self._cache[domain]

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)

        async with self._sem:
            try:
                resp = await self._client.get(
                    API_URL,
                    params={"target": domain},
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Accept": "application/json",
                    },
                )
            except httpx.HTTPError as exc:
                log.warning("Ahrefs request failed for %s: %s", domain, exc)
                self._cache[domain] = None
                return None

        if resp.status_code in (401, 403):
            # A bad key will fail for every domain -- stop hammering it.
            self._disabled = True
            self._disabled_reason = (
                "Ahrefs rejected the API key (HTTP %d). Domain Rating is off "
                "for this run." % resp.status_code
            )
            log.warning(self._disabled_reason)
            return None
        if resp.status_code == 429:
            self._disabled = True
            self._disabled_reason = (
                "Ahrefs rate limit reached. Domain Rating is off for the rest "
                "of this run."
            )
            log.warning(self._disabled_reason)
            return None
        if resp.status_code != 200:
            log.info("Ahrefs HTTP %d for %s", resp.status_code, domain)
            self._cache[domain] = None
            return None

        try:
            body = resp.json()
            value = body["domain_rating"]["domain_rating"]
            dr = float(value) if value is not None else None
        except (ValueError, KeyError, TypeError):
            log.warning("Unexpected Ahrefs payload for %s", domain)
            dr = None

        self._cache[domain] = dr
        return dr
