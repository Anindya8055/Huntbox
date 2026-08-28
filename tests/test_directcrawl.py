"""DirectCrawlClient: the free in-house fallback that fetches a resolved
domain's own likely contact pages when Apify/Serper found a domain but no
email. All HTTP is mocked -- nothing here touches the network."""

import httpx
import pytest

from app.enrichment.directcrawl import DirectCrawlClient, decode_cf_email
from tests.test_clients import patch_transport


def test_decode_cf_email_known_vector():
    # hello@example.com XORed with key 0x2a, key prepended -- see
    # app/enrichment/directcrawl.py's decode_cf_email docstring.
    assert decode_cf_email("2a424f4646456a4f524b475a464f04494547") == "hello@example.com"


def test_decode_cf_email_rejects_garbage():
    assert decode_cf_email("not-hex") == ""
    assert decode_cf_email("2a") == ""  # key byte only, nothing to decode


class TestDirectCrawlClient:
    async def test_extracts_mailto_link(self, monkeypatch):
        def handler(request):
            if request.url.path == "/contact":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    text='<a href="mailto:hello@acme.com">Email us</a>',
                )
            return httpx.Response(404)

        patch_transport(monkeypatch, handler, "app.enrichment.directcrawl.httpx.AsyncClient")
        client = DirectCrawlClient()
        email, verified, source = await client.find_email("acme.com", "Acme")
        await client.aclose()

        assert email == "hello@acme.com"
        assert verified is True
        assert source == "directcrawl"

    async def test_decodes_cloudflare_obfuscated_email(self, monkeypatch):
        encoded = "2a424f4646456a4b49474f04494547"  # -> hello@acme.com

        def handler(request):
            if request.url.path == "/":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    text=f'<span class="__cf_email__" data-cfemail="{encoded}">[email protected]</span>',
                )
            return httpx.Response(404)

        patch_transport(monkeypatch, handler, "app.enrichment.directcrawl.httpx.AsyncClient")
        client = DirectCrawlClient()
        email, verified, source = await client.find_email("acme.com", "Acme")
        await client.aclose()

        assert email == "hello@acme.com"
        assert verified is True

    async def test_deobfuscates_at_dot_text(self, monkeypatch):
        def handler(request):
            if request.url.path == "/about":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    text="<p>Reach us at hello [at] acme [dot] com any time.</p>",
                )
            return httpx.Response(404)

        patch_transport(monkeypatch, handler, "app.enrichment.directcrawl.httpx.AsyncClient")
        client = DirectCrawlClient()
        email, verified, source = await client.find_email("acme.com", "Acme")
        await client.aclose()

        assert email == "hello@acme.com"

    async def test_falls_back_to_mx_checked_guess_when_nothing_found(self, monkeypatch):
        def handler(request):
            if "dns.google" in str(request.url):
                return httpx.Response(200, json={"Answer": [{"data": "10 mail.acme.com."}]})
            return httpx.Response(404)

        patch_transport(monkeypatch, handler, "app.enrichment.directcrawl.httpx.AsyncClient")
        client = DirectCrawlClient()
        email, verified, source = await client.find_email("acme.com", "Acme")
        await client.aclose()

        assert email == "hello@acme.com"
        assert verified is False
        assert source == "guessed"

    async def test_no_guess_when_domain_has_no_mx(self, monkeypatch):
        def handler(request):
            if "dns.google" in str(request.url):
                return httpx.Response(200, json={})  # no Answer -- no MX
            return httpx.Response(404)

        patch_transport(monkeypatch, handler, "app.enrichment.directcrawl.httpx.AsyncClient")
        client = DirectCrawlClient()
        email, verified, source = await client.find_email("acme.com", "Acme")
        await client.aclose()

        assert email == ""
        assert source == ""

    async def test_guess_fallback_can_be_disabled(self, monkeypatch):
        def handler(request):
            if "dns.google" in str(request.url):
                return httpx.Response(200, json={"Answer": [{"data": "10 mail.acme.com."}]})
            return httpx.Response(404)

        patch_transport(monkeypatch, handler, "app.enrichment.directcrawl.httpx.AsyncClient")
        client = DirectCrawlClient(guess_fallback=False)
        email, verified, source = await client.find_email("acme.com", "Acme")
        await client.aclose()

        assert email == ""

    async def test_ignores_off_domain_email_from_third_party_widget(self, monkeypatch):
        def handler(request):
            if request.url.path == "/":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    text='<a href="mailto:support@some-livechat-vendor.com">Chat</a>',
                )
            if "dns.google" in str(request.url):
                return httpx.Response(200, json={})
            return httpx.Response(404)

        patch_transport(monkeypatch, handler, "app.enrichment.directcrawl.httpx.AsyncClient")
        client = DirectCrawlClient()
        email, verified, source = await client.find_email("acme.com", "Acme")
        await client.aclose()

        assert email == ""

    async def test_empty_domain_returns_nothing(self):
        client = DirectCrawlClient()
        email, verified, source = await client.find_email("")
        await client.aclose()
        assert (email, verified, source) == ("", False, "")

    async def test_result_is_cached_per_domain(self, monkeypatch):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            if request.url.path == "/":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    text='<a href="mailto:hello@acme.com">Email us</a>',
                )
            return httpx.Response(404)

        patch_transport(monkeypatch, handler, "app.enrichment.directcrawl.httpx.AsyncClient")
        client = DirectCrawlClient()
        await client.find_email("acme.com", "Acme")
        first_call_count = len(calls)
        await client.find_email("acme.com", "Acme")
        await client.aclose()

        assert len(calls) == first_call_count  # second call served from cache
