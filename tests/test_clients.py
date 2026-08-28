"""Product Hunt and Serper clients, driven by mocked HTTP responses.

Nothing here touches the network: httpx.AsyncClient is replaced with a
MockTransport that replays canned payloads.
"""

import httpx
import pytest

from app.enrichment.directcrawl import DirectCrawlClient
from app.enrichment.serper import SerperProvider, SerperQuotaError
from app.models import Product
from app.producthunt import ProductHuntClient, ProductHuntError
from app.timeframes import DateRange

pytestmark = pytest.mark.asyncio

RANGE = DateRange.__call__(  # a single-day range
    __import__("datetime").date(2026, 8, 22),
    __import__("datetime").date(2026, 8, 22),
)


def ph_node(name: str, votes: int) -> dict:
    return {
        "id": "1",
        "name": name,
        "tagline": f"{name} tagline",
        "description": f"{name} description",
        "votesCount": votes,
        "commentsCount": 3,
        "createdAt": "2026-08-22T09:00:00Z",
        "url": f"https://www.producthunt.com/products/{name.lower()}",
        "website": "https://www.producthunt.com/r/ABC123",
        "topics": {"edges": [{"node": {"name": "AI"}}]},
    }


def ph_payload(nodes: list[dict], has_next: bool = False, cursor: str = "") -> dict:
    return {
        "data": {
            "posts": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "edges": [{"node": n} for n in nodes],
            }
        }
    }


def patch_transport(monkeypatch, handler, target: str) -> None:
    """Force the module's AsyncClient to use a MockTransport."""
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(target, factory)


class TestProductHuntClient:
    async def test_missing_token_is_reported_not_raised_late(self):
        ok, reason = ProductHuntClient(None).available()
        assert not ok
        assert "PRODUCTHUNT_API_TOKEN" in reason

    async def test_parses_posts_and_assigns_ranks(self, monkeypatch):
        def handler(request):
            assert request.headers["Authorization"] == "Bearer tok"
            return httpx.Response(200, json=ph_payload([ph_node("Alpha", 90), ph_node("Beta", 50)]))

        patch_transport(monkeypatch, handler, "app.producthunt.httpx.AsyncClient")
        posts = await ProductHuntClient("tok").top_posts(RANGE, 10)

        assert [p.rank for p in posts] == [1, 2]
        assert posts[0].product_name == "Alpha"
        assert posts[0].votes == 90
        assert posts[0].topics == ["AI"]
        assert posts[0].launch_date == "2026-08-22"

    async def test_paginates_until_limit_is_reached(self, monkeypatch):
        calls = []

        def handler(request):
            import json as _json

            body = _json.loads(request.content)
            calls.append(body["variables"]["after"])
            if len(calls) == 1:
                return httpx.Response(
                    200, json=ph_payload([ph_node(f"P{i}", 100 - i) for i in range(3)],
                                         has_next=True, cursor="CUR1")
                )
            return httpx.Response(200, json=ph_payload([ph_node("P3", 40)]))

        patch_transport(monkeypatch, handler, "app.producthunt.httpx.AsyncClient")
        posts = await ProductHuntClient("tok").top_posts(RANGE, 4)

        assert len(posts) == 4
        assert calls == [None, "CUR1"]  # second page used the cursor
        assert [p.rank for p in posts] == [1, 2, 3, 4]

    async def test_stops_when_api_runs_out_early(self, monkeypatch):
        def handler(request):
            return httpx.Response(200, json=ph_payload([ph_node("Only", 10)]))

        patch_transport(monkeypatch, handler, "app.producthunt.httpx.AsyncClient")
        posts = await ProductHuntClient("tok").top_posts(RANGE, 50)
        assert len(posts) == 1

    async def test_empty_range_yields_no_posts(self, monkeypatch):
        def handler(request):
            return httpx.Response(200, json=ph_payload([]))

        patch_transport(monkeypatch, handler, "app.producthunt.httpx.AsyncClient")
        assert await ProductHuntClient("tok").top_posts(RANGE, 10) == []

    @pytest.mark.parametrize(
        "status,fragment",
        [(401, "token"), (403, "token"), (429, "rate limit"), (500, "trouble")],
    )
    async def test_http_errors_become_readable_messages(self, monkeypatch, status, fragment):
        def handler(request):
            return httpx.Response(status, json={})

        patch_transport(monkeypatch, handler, "app.producthunt.httpx.AsyncClient")
        with pytest.raises(ProductHuntError) as exc:
            await ProductHuntClient("tok").top_posts(RANGE, 5)
        assert fragment in str(exc.value).lower()

    async def test_graphql_errors_surface_the_api_message(self, monkeypatch):
        def handler(request):
            return httpx.Response(200, json={"errors": [{"message": "Field 'nope' doesn't exist"}]})

        patch_transport(monkeypatch, handler, "app.producthunt.httpx.AsyncClient")
        with pytest.raises(ProductHuntError, match="doesn't exist"):
            await ProductHuntClient("tok").top_posts(RANGE, 5)

    async def test_timeout_is_wrapped(self, monkeypatch):
        def handler(request):
            raise httpx.TimeoutException("too slow")

        patch_transport(monkeypatch, handler, "app.producthunt.httpx.AsyncClient")
        with pytest.raises(ProductHuntError, match="too long"):
            await ProductHuntClient("tok").top_posts(RANGE, 5)


def serper_payload(organic: list[dict]) -> dict:
    return {"organic": organic}


def product(name="Toplify", tagline="Track your App Store ranking") -> Product:
    return Product(rank=1, product_name=name, tagline=tagline, description="short")


class TestSerperProvider:
    async def test_missing_key_is_reported(self):
        ok, reason = SerperProvider(None).available()
        assert not ok
        assert "SERPER_API_KEY" in reason

    async def test_happy_path_resolves_domain_then_email(self, monkeypatch):
        seen = []

        def handler(request):
            import json as _json

            q = _json.loads(request.content)["q"]
            seen.append(q)
            if "Track your App Store" in q:  # strategy 0: discovery
                return httpx.Response(200, json=serper_payload([
                    {"title": "Toplify", "link": "https://toplify.app", "snippet": "Rank tracker"},
                ]))
            if q.startswith("site:"):  # strategy 1
                return httpx.Response(200, json=serper_payload([
                    {"title": "Contact", "link": "https://toplify.app/contact",
                     "snippet": "Reach us at hello@toplify.app anytime."},
                ]))
            return httpx.Response(200, json=serper_payload([]))

        patch_transport(monkeypatch, handler, "app.enrichment.serper.httpx.AsyncClient")
        p = SerperProvider("key", delay_seconds=0)
        result = await p.enrich(product())
        await p.aclose()

        assert result.domain == "toplify.app"
        assert result.email == "hello@toplify.app"
        assert result.email_verified is True
        assert seen[0].startswith('"Toplify"')  # discovery ran first

    async def test_rejects_a_namesake_domain(self, monkeypatch):
        """The live regression: top result was threads.com for Toplify."""

        def handler(request):
            import json as _json

            q = _json.loads(request.content)["q"]
            if "Track your App Store" in q:
                return httpx.Response(200, json=serper_payload([
                    {"title": "Threads", "link": "https://threads.com", "snippet": "Meta app"},
                ]))
            return httpx.Response(200, json=serper_payload([
                {"title": "Some other co", "link": "https://appps.od.ua",
                 "snippet": "Contact support@appps.od.ua"},
            ]))

        patch_transport(monkeypatch, handler, "app.enrichment.serper.httpx.AsyncClient")
        p = SerperProvider("key", delay_seconds=0)
        result = await p.enrich(product())
        await p.aclose()

        assert result.domain == ""            # namesake rejected
        assert result.email == ""             # unrelated address not adopted
        assert result.email_verified is False

    async def test_unverified_when_name_matches_but_no_domain(self, monkeypatch):
        def handler(request):
            import json as _json

            q = _json.loads(request.content)["q"]
            if "AI-Native" in q:  # discovery finds only a blog host
                return httpx.Response(200, json=serper_payload([
                    {"title": "post", "link": "https://dev.to/x", "snippet": "about it"},
                ]))
            return httpx.Response(200, json=serper_payload([
                {"title": "Open Analytics", "link": "https://open-analytics.com.au",
                 "snippet": "Email contact@open-analytics.com.au"},
            ]))

        patch_transport(monkeypatch, handler, "app.enrichment.serper.httpx.AsyncClient")
        p = SerperProvider("key", delay_seconds=0)
        result = await p.enrich(product("Open Analytics", "AI-Native analytics"))
        await p.aclose()

        assert result.email == "contact@open-analytics.com.au"
        assert result.email_verified is False
        assert "unverified" in result.note

    async def test_no_email_found_leaves_field_empty(self, monkeypatch):
        def handler(request):
            return httpx.Response(200, json=serper_payload([
                {"title": "Toplify", "link": "https://toplify.app", "snippet": "No contact here"},
            ]))

        patch_transport(monkeypatch, handler, "app.enrichment.serper.httpx.AsyncClient")
        p = SerperProvider("key", delay_seconds=0)
        result = await p.enrich(product())
        await p.aclose()

        assert result.email == ""
        assert result.domain == "toplify.app"

    async def test_domain_cache_prevents_duplicate_lookups(self, monkeypatch):
        count = {"n": 0}

        def handler(request):
            count["n"] += 1
            return httpx.Response(200, json=serper_payload([
                {"title": "Toplify", "link": "https://toplify.app",
                 "snippet": "hello@toplify.app"},
            ]))

        patch_transport(monkeypatch, handler, "app.enrichment.serper.httpx.AsyncClient")
        p = SerperProvider("key", delay_seconds=0)
        await p.enrich(product())
        first = count["n"]
        await p.enrich(product())  # same product again
        await p.aclose()

        assert count["n"] == first, "second lookup should be served from cache"

    async def test_rate_limit_raises_quota_error(self, monkeypatch):
        def handler(request):
            return httpx.Response(429, json={"message": "rate limited"})

        patch_transport(monkeypatch, handler, "app.enrichment.serper.httpx.AsyncClient")
        p = SerperProvider("key", delay_seconds=0)
        with pytest.raises(SerperQuotaError):
            await p.enrich(product())
        await p.aclose()

    async def test_bad_key_raises_quota_error(self, monkeypatch):
        def handler(request):
            return httpx.Response(403, json={"message": "forbidden"})

        patch_transport(monkeypatch, handler, "app.enrichment.serper.httpx.AsyncClient")
        p = SerperProvider("key", delay_seconds=0)
        with pytest.raises(SerperQuotaError, match="SERPER_API_KEY"):
            await p.enrich(product())
        await p.aclose()

    async def test_rejected_query_pattern_skips_that_strategy(self, monkeypatch):
        """Serper 400s on some free-tier patterns; the product should survive."""

        def handler(request):
            import json as _json

            q = _json.loads(request.content)["q"]
            if q.startswith("site:"):
                return httpx.Response(400, json={"message": "Query pattern not allowed"})
            if "Track your App Store" in q:
                return httpx.Response(200, json=serper_payload([
                    {"title": "Toplify", "link": "https://toplify.app", "snippet": "x"},
                ]))
            return httpx.Response(200, json=serper_payload([
                {"title": "Contact", "link": "https://toplify.app/c",
                 "snippet": "hello@toplify.app"},
            ]))

        patch_transport(monkeypatch, handler, "app.enrichment.serper.httpx.AsyncClient")
        p = SerperProvider("key", delay_seconds=0)
        result = await p.enrich(product())
        await p.aclose()

        assert result.email == "hello@toplify.app"

    async def test_domain_but_no_snippet_email_falls_through_to_directcrawl(self, monkeypatch):
        """Google's snippets never carry mailto: links -- once a domain is
        resolved but the snippet queries find nothing, one more free pass
        over the site itself should still turn up an address."""

        def handler(request):
            if "dns.google" in str(request.url):
                return httpx.Response(200, json={})
            if request.url.host == "toplify.app" and request.url.path == "/":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    text='<a href="mailto:hello@toplify.app">Email</a>',
                )
            if "google.serper.dev" in str(request.url):
                return httpx.Response(200, json=serper_payload([
                    {"title": "Toplify", "link": "https://toplify.app", "snippet": "no address here"},
                ]))
            return httpx.Response(404)

        patch_transport(monkeypatch, handler, "app.enrichment.serper.httpx.AsyncClient")
        patch_transport(monkeypatch, handler, "app.enrichment.directcrawl.httpx.AsyncClient")
        directcrawl = DirectCrawlClient()
        p = SerperProvider("key", delay_seconds=0, directcrawl=directcrawl)
        result = await p.enrich(product())
        await p.aclose()

        assert result.domain == "toplify.app"
        assert result.email == "hello@toplify.app"
        assert result.email_source == "directcrawl"
