"""The swappable enrichment provider interface (Stage 2).

Any provider -- Serper today, a direct-site scraper or Hunter.io later --
implements this protocol, so Stage 1 and the API layer never change.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.models import Enrichment, Product


@runtime_checkable
class EnrichmentProvider(Protocol):
    name: str

    def available(self) -> tuple[bool, str]:
        """(is_usable, reason_if_not) -- checked before a run starts."""

    async def enrich(self, product: Product) -> Enrichment:
        """Look up company details + a contact email for one product."""

    async def aclose(self) -> None:
        """Release any held HTTP resources."""
