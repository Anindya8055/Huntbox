"""JobRegistry._enrich_all: the cross-run company cache short-circuit.

A confirmed email from a prior run should be reused for a resurfacing
product (same PH redirect URL) without calling the provider again; a fresh
find should be written back to the cache so the next run can reuse it.
"""

import pytest

from app.jobs import JobRegistry
from app.models import Enrichment, JobStatus, Product
from app.storage import Storage

pytestmark = pytest.mark.asyncio


def product(
    website_url="https://www.producthunt.com/r/ABC",
    producthunt_url="https://www.producthunt.com/products/toplify",
) -> Product:
    return Product(
        rank=1, product_name="Toplify", tagline="Track ranking",
        description="short", website_url=website_url, producthunt_url=producthunt_url,
    )


def job() -> JobStatus:
    return JobStatus(job_id="job1", state="enriching")


class StubProvider:
    """Records every call so tests can assert the cache actually skipped it."""

    name = "stub"

    def __init__(self, result: Enrichment) -> None:
        self.result = result
        self.calls = 0

    def available(self) -> tuple[bool, str]:
        return True, ""

    async def enrich(self, product: Product) -> Enrichment:
        self.calls += 1
        return self.result

    async def aclose(self) -> None:
        pass


class StubPHPage:
    def __init__(self, domain: str) -> None:
        self.domain = domain
        self.calls = 0

    async def resolve_domain(self, product: Product) -> str:
        self.calls += 1
        return self.domain

    async def aclose(self) -> None:
        pass


class StubDirectCrawl:
    def __init__(self, result: tuple[str, bool, str]) -> None:
        self.result = result
        self.calls = 0

    async def find_email(self, domain: str, product_name: str = "") -> tuple[str, bool, str]:
        self.calls += 1
        return self.result

    async def aclose(self) -> None:
        pass


class TestPHPageFreePath:
    """PH's own page can resolve a domain (and, via directcrawl, an email)
    for a same-day launch Google hasn't indexed yet -- entirely for free,
    without ever calling the paid provider."""

    async def test_ph_page_plus_directcrawl_skips_provider_entirely(self, tmp_path):
        reg = JobRegistry()
        await reg.attach_storage(Storage(tmp_path / "t.db"))
        provider = StubProvider(Enrichment())  # would return nothing if called
        ph_page = StubPHPage("fideisland.it.com")
        directcrawl = StubDirectCrawl(("hello@fideisland.it.com", True, "directcrawl"))
        p = product()

        await reg._enrich_all(job(), [p], provider, ph_page=ph_page, directcrawl=directcrawl)

        assert provider.calls == 0
        assert ph_page.calls == 1
        assert p.domain == "fideisland.it.com"
        assert p.email == "hello@fideisland.it.com"
        assert p.email_verified is True
        assert p.enrichment_status == "found"

    async def test_ph_page_domain_falls_through_to_provider_when_no_email(self, tmp_path):
        """A resolved domain with no email found by directcrawl still lets
        Apify/Serper run -- now against the known-correct domain."""
        reg = JobRegistry()
        await reg.attach_storage(Storage(tmp_path / "t.db"))
        provider = StubProvider(Enrichment(
            domain="fideisland.it.com", email="press@fideisland.it.com",
            email_verified=True, email_source="apify",
        ))
        ph_page = StubPHPage("fideisland.it.com")
        directcrawl = StubDirectCrawl(("", False, ""))
        p = product()

        await reg._enrich_all(job(), [p], provider, ph_page=ph_page, directcrawl=directcrawl)

        assert provider.calls == 1
        assert p.domain == "fideisland.it.com"
        assert p.email == "press@fideisland.it.com"

    async def test_ph_page_finding_nothing_falls_through_to_provider(self, tmp_path):
        """PH's page not carrying a usable link is a routine miss -- the
        provider still runs normally, unaffected."""
        reg = JobRegistry()
        await reg.attach_storage(Storage(tmp_path / "t.db"))
        provider = StubProvider(Enrichment(
            domain="toplify.app", email="hello@toplify.app",
            email_verified=True, email_source="apify",
        ))
        ph_page = StubPHPage("")  # nothing found on PH's own page
        p = product()

        await reg._enrich_all(job(), [p], provider, ph_page=ph_page)

        assert provider.calls == 1
        assert p.domain == "toplify.app"


class TestCompanyCache:
    async def test_fresh_find_is_written_to_cache(self, tmp_path):
        reg = JobRegistry()
        await reg.attach_storage(Storage(tmp_path / "t.db"))
        provider = StubProvider(Enrichment(
            company_name="Toplify", domain="toplify.app",
            email="hello@toplify.app", email_verified=True,
            email_source="apify", note="domain+email via apify",
        ))

        await reg._enrich_all(job(), [product()], provider)

        cached = await reg.storage.cached_enrichment("https://www.producthunt.com/r/ABC")
        assert cached is not None
        assert cached["email"] == "hello@toplify.app"
        assert cached["email_source"] == "apify"

    async def test_cache_hit_skips_the_provider_entirely(self, tmp_path):
        reg = JobRegistry()
        await reg.attach_storage(Storage(tmp_path / "t.db"))
        await reg.storage.upsert_cached_enrichment(
            "https://www.producthunt.com/r/ABC",
            {
                "domain": "toplify.app",
                "company_name": "Toplify",
                "company_description": "App ranking tracker",
                "email": "hello@toplify.app",
                "email_verified": True,
                "email_source": "apify",
                "note": "domain+email via apify",
            },
        )
        provider = StubProvider(Enrichment())  # would return nothing if called
        p = product()

        await reg._enrich_all(job(), [p], provider)

        assert provider.calls == 0
        assert p.email == "hello@toplify.app"
        assert p.domain == "toplify.app"
        assert p.enrichment_status == "found"
        assert "cached" in p.enrichment_note

    async def test_serper_fallback_cache_is_rechecked(self, tmp_path):
        """A cached fallback result must not preserve a stale/false domain."""
        reg = JobRegistry()
        await reg.attach_storage(Storage(tmp_path / "t.db"))
        await reg.storage.upsert_cached_enrichment(
            "https://www.producthunt.com/r/ABC",
            {
                "domain": "idcrawl.com",
                "email": "hello@idcrawl.com",
                "email_verified": True,
                "email_source": "directcrawl",
                "note": "email via directcrawl; resolved via serper fallback",
            },
        )
        provider = StubProvider(Enrichment(
            company_name="Toplify", domain="toplify.app",
            email="hello@toplify.app", email_verified=True,
            email_source="apify", note="domain+email via apify",
        ))

        p = product()
        await reg._enrich_all(job(), [p], provider)

        assert provider.calls == 1
        assert p.domain == "toplify.app"

    async def test_no_email_result_is_not_cached(self, tmp_path):
        """A miss stays uncached so a later run still gets a fresh shot."""
        reg = JobRegistry()
        await reg.attach_storage(Storage(tmp_path / "t.db"))
        provider = StubProvider(Enrichment(domain="toplify.app", email=""))

        await reg._enrich_all(job(), [product()], provider)

        assert await reg.storage.cached_enrichment("https://www.producthunt.com/r/ABC") is None

    async def test_no_website_url_never_touches_cache(self, tmp_path):
        reg = JobRegistry()
        await reg.attach_storage(Storage(tmp_path / "t.db"))
        provider = StubProvider(Enrichment(domain="toplify.app", email="hello@toplify.app"))

        await reg._enrich_all(job(), [product(website_url="")], provider)

        assert provider.calls == 1  # cache lookup was skipped, provider still ran
