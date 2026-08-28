"""PHPageResolver: reads a product's real domain straight off its own
Product Hunt page -- no search index, no paid actor. All HTTP is mocked."""

import httpx
import pytest

from app.enrichment.ph_page import PHPageResolver
from app.models import Product
from tests.test_clients import patch_transport

pytestmark = pytest.mark.asyncio


def product(producthunt_url="https://www.producthunt.com/products/fide-island") -> Product:
    return Product(
        rank=17, product_name="Fide Island", tagline="Make your MacBook notch actually useful",
        description="short", producthunt_url=producthunt_url,
    )


class TestPHPageResolver:
    async def test_resolves_via_visit_website_anchor(self, monkeypatch):
        html = """
        <html><body>
        <a href="https://fideisland.it.com/?ref=producthunt" rel="nofollow noopener">Visit website</a>
        </body></html>
        """

        def handler(request):
            return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

        patch_transport(monkeypatch, handler, "app.enrichment.ph_page.httpx.AsyncClient")
        resolver = PHPageResolver()
        domain = await resolver.resolve_domain(product())
        await resolver.aclose()

        assert domain == "fideisland.it.com"

    async def test_resolves_via_next_data_when_no_visit_anchor(self, monkeypatch):
        html = """
        <html><body>
        <script id="__NEXT_DATA__" type="application/json">
        {"props": {"pageProps": {"post": {"website": "https://realsite.example/"}}}}
        </script>
        </body></html>
        """

        def handler(request):
            return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

        patch_transport(monkeypatch, handler, "app.enrichment.ph_page.httpx.AsyncClient")
        resolver = PHPageResolver()
        domain = await resolver.resolve_domain(product())
        await resolver.aclose()

        assert domain == "realsite.example"

    async def test_ignores_social_links_and_ph_own_domain(self, monkeypatch):
        html = """
        <html><body>
        <a href="https://twitter.com/fideisland" rel="nofollow">Follow us</a>
        <a href="https://www.producthunt.com/products/fide-island">See more</a>
        </body></html>
        """

        def handler(request):
            return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

        patch_transport(monkeypatch, handler, "app.enrichment.ph_page.httpx.AsyncClient")
        resolver = PHPageResolver()
        domain = await resolver.resolve_domain(product())
        await resolver.aclose()

        assert domain == ""

    async def test_no_match_returns_empty(self, monkeypatch):
        def handler(request):
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html></html>")

        patch_transport(monkeypatch, handler, "app.enrichment.ph_page.httpx.AsyncClient")
        resolver = PHPageResolver()
        domain = await resolver.resolve_domain(product())
        await resolver.aclose()

        assert domain == ""

    async def test_fetch_failure_returns_empty_not_raises(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("boom")

        patch_transport(monkeypatch, handler, "app.enrichment.ph_page.httpx.AsyncClient")
        resolver = PHPageResolver()
        domain = await resolver.resolve_domain(product())
        await resolver.aclose()

        assert domain == ""

    async def test_no_producthunt_url_returns_empty_without_a_request(self, monkeypatch):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, text="<html></html>")

        patch_transport(monkeypatch, handler, "app.enrichment.ph_page.httpx.AsyncClient")
        resolver = PHPageResolver()
        domain = await resolver.resolve_domain(product(producthunt_url=""))
        await resolver.aclose()

        assert domain == ""
        assert calls == []

    async def test_result_is_cached_per_producthunt_url(self, monkeypatch):
        calls = []
        html = '<a href="https://fideisland.it.com/" rel="nofollow">Visit website</a>'

        def handler(request):
            calls.append(request)
            return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

        patch_transport(monkeypatch, handler, "app.enrichment.ph_page.httpx.AsyncClient")
        resolver = PHPageResolver()
        await resolver.resolve_domain(product())
        first_count = len(calls)
        await resolver.resolve_domain(product())
        await resolver.aclose()

        assert len(calls) == first_count
