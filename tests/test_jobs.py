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


def product(website_url="https://www.producthunt.com/r/ABC") -> Product:
    return Product(
        rank=1, product_name="Toplify", tagline="Track ranking",
        description="short", website_url=website_url,
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
