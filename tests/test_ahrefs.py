"""Ahrefs Domain Rating client, driven by mocked HTTP responses."""

import httpx
import pytest

from app.enrichment.ahrefs import AhrefsClient
from tests.test_clients import patch_transport

pytestmark = pytest.mark.asyncio

TARGET = "app.enrichment.ahrefs.httpx.AsyncClient"


def dr_response(value):
    return {"domain_rating": {"domain_rating": value, "license": "https://ahrefs.com/x"}}


class TestAvailability:
    async def test_missing_key_is_reported(self):
        ok, reason = AhrefsClient(None).available()
        assert not ok
        assert "AHREFS_API_KEY" in reason

    async def test_key_present(self):
        assert AhrefsClient("k").available() == (True, "")

    async def test_no_key_yields_none_without_calling_out(self):
        assert await AhrefsClient(None).domain_rating("acme.io") is None


class TestLookup:
    async def test_parses_domain_rating(self, monkeypatch):
        def handler(request):
            assert request.headers["Authorization"] == "Bearer key"
            assert "target=acme.io" in str(request.url)
            return httpx.Response(200, json=dr_response(72.0))

        patch_transport(monkeypatch, handler, TARGET)
        c = AhrefsClient("key")
        assert await c.domain_rating("acme.io") == 72.0
        await c.aclose()

    async def test_zero_is_a_real_value_not_missing(self, monkeypatch):
        patch_transport(monkeypatch, lambda r: httpx.Response(200, json=dr_response(0.0)), TARGET)
        c = AhrefsClient("key")
        assert await c.domain_rating("new.io") == 0.0
        await c.aclose()

    async def test_null_rating_becomes_none(self, monkeypatch):
        patch_transport(monkeypatch, lambda r: httpx.Response(200, json=dr_response(None)), TARGET)
        c = AhrefsClient("key")
        assert await c.domain_rating("acme.io") is None
        await c.aclose()

    async def test_empty_domain_short_circuits(self):
        assert await AhrefsClient("key").domain_rating("") is None

    async def test_results_are_cached_per_domain(self, monkeypatch):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, json=dr_response(50.0))

        patch_transport(monkeypatch, handler, TARGET)
        c = AhrefsClient("key")
        await c.domain_rating("acme.io")
        await c.domain_rating("acme.io")
        await c.aclose()
        assert calls["n"] == 1


class TestFailureIsAlwaysSoft:
    """DR is a nice-to-have column; nothing here may raise."""

    @pytest.mark.parametrize("status", [400, 404, 500, 503])
    async def test_http_errors_return_none(self, monkeypatch, status):
        patch_transport(monkeypatch, lambda r: httpx.Response(status, json={}), TARGET)
        c = AhrefsClient("key")
        assert await c.domain_rating("acme.io") is None
        await c.aclose()

    async def test_transport_error_returns_none(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("no route")

        patch_transport(monkeypatch, handler, TARGET)
        c = AhrefsClient("key")
        assert await c.domain_rating("acme.io") is None
        await c.aclose()

    async def test_malformed_payload_returns_none(self, monkeypatch):
        patch_transport(monkeypatch, lambda r: httpx.Response(200, json={"nope": 1}), TARGET)
        c = AhrefsClient("key")
        assert await c.domain_rating("acme.io") is None
        await c.aclose()

    @pytest.mark.parametrize("status,fragment", [(401, "key"), (403, "key"), (429, "rate limit")])
    async def test_auth_and_quota_failures_disable_for_the_run(
        self, monkeypatch, status, fragment
    ):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(status, json={})

        patch_transport(monkeypatch, handler, TARGET)
        c = AhrefsClient("key")
        assert await c.domain_rating("a.io") is None
        assert await c.domain_rating("b.io") is None  # must not call again
        await c.aclose()

        assert calls["n"] == 1, "client should stop after an auth/quota failure"
        assert fragment in c.disabled_reason.lower()
