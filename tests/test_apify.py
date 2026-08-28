"""Apify Contact Info Scraper provider + Serper fallback, and the RDAP
domain-age client -- all driven by mocked HTTP responses."""

import httpx
import pytest

from app.enrichment.apify import ApifyContactProvider
from app.enrichment.directcrawl import DirectCrawlClient
from app.enrichment.domain_age import DomainAgeClient
from app.enrichment.serper import SerperProvider
from app.models import Product

from tests.test_clients import patch_transport, serper_payload

pytestmark = pytest.mark.asyncio


def product(name="Toplify", website="https://www.producthunt.com/r/ABC123") -> Product:
    return Product(rank=1, product_name=name, tagline="Track your App Store ranking",
                    description="short", website_url=website)


class TestApifyContactProvider:
    async def test_no_website_url_skips_straight_to_serper(self, monkeypatch):
        def serper_handler(request):
            return httpx.Response(200, json=serper_payload([
                {"title": "Toplify", "link": "https://toplify.app", "snippet": "hello@toplify.app"},
            ]))

        patch_transport(monkeypatch, serper_handler, "app.enrichment.serper.httpx.AsyncClient")
        serper = SerperProvider("key", delay_seconds=0)
        provider = ApifyContactProvider("apify-token", serper)

        result = await provider.enrich(product(website=""))
        await provider.aclose()

        assert result.domain == "toplify.app"

    async def test_apify_resolves_domain_and_email_directly(self, monkeypatch):
        def apify_handler(request):
            # run-sync-get-dataset-items returns 201 Created on success, not 200.
            return httpx.Response(201, json=[
                {"domain": "toplify.app", "emails": ["hello@toplify.app"]},
            ])

        patch_transport(monkeypatch, apify_handler, "app.enrichment.apify.httpx.AsyncClient")
        serper = SerperProvider(None)  # never called -- unavailable is fine, it's not touched
        provider = ApifyContactProvider("apify-token", serper)

        result = await provider.enrich(product())
        await provider.aclose()

        assert result.domain == "toplify.app"
        assert result.email == "hello@toplify.app"
        assert result.email_verified is True

    async def test_ignores_actors_stale_domain_field_uses_scraped_urls(self, monkeypatch):
        """Regression: the actor's own "domain" field reports the *original*
        start URL's host, not where the crawl actually landed -- a PH
        redirect comes back with domain="producthunt.com" even though
        scrapedUrls show it correctly followed through to the real site.
        """
        def apify_handler(request):
            return httpx.Response(201, json=[{
                "domain": "producthunt.com",
                "scrapedUrls": ["https://www.paymentkit.com/", "https://www.paymentkit.com/pricing/"],
                "emails": ["hello@paymentkit.com"],
            }])

        patch_transport(monkeypatch, apify_handler, "app.enrichment.apify.httpx.AsyncClient")
        serper = SerperProvider(None)
        provider = ApifyContactProvider("apify-token", serper)

        result = await provider.enrich(product(name="PaymentKit"))
        await provider.aclose()

        assert result.domain == "paymentkit.com"
        assert result.email == "hello@paymentkit.com"

    async def test_falls_back_to_serper_when_apify_finds_nothing(self, monkeypatch):
        # apify.py and serper.py both `import httpx`, sharing the same module
        # object, so a single dispatching handler stands in for both clients.
        def handler(request):
            if "apify.com" in str(request.url):
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=serper_payload([
                {"title": "Toplify", "link": "https://toplify.app", "snippet": "hello@toplify.app"},
            ]))

        patch_transport(monkeypatch, handler, "app.enrichment.apify.httpx.AsyncClient")
        serper = SerperProvider("key", delay_seconds=0)
        provider = ApifyContactProvider("apify-token", serper)

        result = await provider.enrich(product())
        await provider.aclose()

        assert result.domain == "toplify.app"
        assert "serper fallback" in result.note

    async def test_falls_back_to_serper_on_apify_timeout(self, monkeypatch):
        def handler(request):
            if "apify.com" in str(request.url):
                raise httpx.TimeoutException("too slow")
            return httpx.Response(200, json=serper_payload([
                {"title": "Toplify", "link": "https://toplify.app", "snippet": "hello@toplify.app"},
            ]))

        patch_transport(monkeypatch, handler, "app.enrichment.apify.httpx.AsyncClient")
        serper = SerperProvider("key", delay_seconds=0)
        provider = ApifyContactProvider("apify-token", serper)

        result = await provider.enrich(product())
        await provider.aclose()

        assert result.domain == "toplify.app"

    async def test_missing_token_is_reported(self):
        ok, reason = ApifyContactProvider(None, SerperProvider(None)).available()
        assert not ok
        assert "APIFY_API_TOKEN" in reason

    async def test_domain_but_no_email_falls_through_to_directcrawl(self, monkeypatch):
        """Apify finds a domain but the actor's own crawl found no email --
        the one more free pass over the site's own contact pages should
        still turn up an address instead of leaving the field blank."""

        def handler(request):
            if "apify.com" in str(request.url):
                return httpx.Response(201, json=[{"domain": "toplify.app", "emails": []}])
            if "dns.google" in str(request.url):
                return httpx.Response(200, json={})
            if request.url.path == "/":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    text='<a href="mailto:hello@toplify.app">Email</a>',
                )
            return httpx.Response(404)

        patch_transport(monkeypatch, handler, "app.enrichment.apify.httpx.AsyncClient")
        patch_transport(monkeypatch, handler, "app.enrichment.directcrawl.httpx.AsyncClient")
        serper = SerperProvider(None)
        directcrawl = DirectCrawlClient()
        provider = ApifyContactProvider("apify-token", serper, directcrawl=directcrawl)

        result = await provider.enrich(product())
        await provider.aclose()
        await directcrawl.aclose()

        assert result.domain == "toplify.app"
        assert result.email == "hello@toplify.app"
        assert result.email_source == "directcrawl"
        assert "email via directcrawl" in result.note

    async def test_apify_timeout_retries_once_before_falling_back(self, monkeypatch):
        attempts = {"n": 0}

        def handler(request):
            if "apify.com" in str(request.url):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise httpx.TimeoutException("too slow")
                return httpx.Response(201, json=[{"domain": "toplify.app", "emails": ["hello@toplify.app"]}])
            return httpx.Response(500)  # Serper should never be reached after a successful retry

        patch_transport(monkeypatch, handler, "app.enrichment.apify.httpx.AsyncClient")
        serper = SerperProvider(None)
        provider = ApifyContactProvider("apify-token", serper)

        result = await provider.enrich(product())
        await provider.aclose()

        assert attempts["n"] == 2
        assert result.email == "hello@toplify.app"


class TestDomainAgeClient:
    async def test_extracts_registration_date_as_years(self, monkeypatch):
        def handler(request):
            return httpx.Response(200, json={
                "events": [{"eventAction": "registration", "eventDate": "2015-01-01T00:00:00Z"}]
            })

        patch_transport(monkeypatch, handler, "app.enrichment.domain_age.httpx.AsyncClient")
        client = DomainAgeClient()
        age = await client.domain_age_years("toplify.app")
        await client.aclose()

        assert age is not None and age > 5

    async def test_missing_registration_event_returns_none(self, monkeypatch):
        def handler(request):
            return httpx.Response(200, json={"events": []})

        patch_transport(monkeypatch, handler, "app.enrichment.domain_age.httpx.AsyncClient")
        client = DomainAgeClient()
        age = await client.domain_age_years("toplify.app")
        await client.aclose()

        assert age is None

    async def test_404_returns_none_without_raising(self, monkeypatch):
        def handler(request):
            return httpx.Response(404, json={})

        patch_transport(monkeypatch, handler, "app.enrichment.domain_age.httpx.AsyncClient")
        client = DomainAgeClient()
        age = await client.domain_age_years("nonexistent.example")
        await client.aclose()

        assert age is None

    async def test_rate_limit_disables_client_for_rest_of_run(self, monkeypatch):
        def handler(request):
            return httpx.Response(429, json={})

        patch_transport(monkeypatch, handler, "app.enrichment.domain_age.httpx.AsyncClient")
        client = DomainAgeClient()
        await client.domain_age_years("a.com")
        await client.aclose()

        assert client.disabled_reason
