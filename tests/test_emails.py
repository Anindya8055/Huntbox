"""Email extraction / validation / ranking against realistic Serper snippets."""

import pytest

from app.enrichment.emails import (
    best_email,
    domain_matches_product,
    extract_emails,
    is_company_host,
    is_plausible_email,
    normalize_domain,
    registrable_domain,
)


class TestNormalizeDomain:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("https://www.acme.io/contact", "acme.io"),
            ("http://acme.io", "acme.io"),
            ("acme.io", "acme.io"),
            ("https://ACME.IO/Path", "acme.io"),
            ("https://blog.acme.co.uk/x?y=1", "blog.acme.co.uk"),
            ("", ""),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_domain(raw) == expected

    @pytest.mark.parametrize(
        "domain,expected",
        [
            ("mail.acme.com", "acme.com"),
            ("acme.com", "acme.com"),
            ("acme.co.uk", "acme.co.uk"),
            ("blog.acme.co.uk", "acme.co.uk"),
        ],
    )
    def test_registrable(self, domain, expected):
        assert registrable_domain(domain) == expected


class TestIsPlausibleEmail:
    @pytest.mark.parametrize(
        "addr",
        [
            "hello@acme.io",
            "contact@sub.acme.co.uk",
            "first.last+tag@acme.com",
            "a@b.co",
        ],
    )
    def test_accepts_real_addresses(self, addr):
        assert is_plausible_email(addr)

    @pytest.mark.parametrize(
        "addr",
        [
            "logo@2x.png",            # asset filename
            "sprite@3x.jpg",
            "you@example.com",        # placeholder
            "name@yourdomain.com",
            "test@example.org",
            "hello@producthunt.com",  # never the company itself
            "not-an-email",
            "two@@at.com",
            "trailing@dot.",
            "a@b",                    # no TLD
            "double..dot@acme.com",
            "",
        ],
    )
    def test_rejects_junk(self, addr):
        assert not is_plausible_email(addr)

    def test_rejects_sentry_style_hex_keys(self):
        assert not is_plausible_email("0a1b2c3d4e5f60718293a4b5c6d7e8f9@sentry.io")


class TestExtractEmails:
    def test_pulls_addresses_out_of_a_snippet(self):
        snippet = (
            "Contact us at hello@acme.io or support@acme.io for help. "
            "Press: press@acme.io."
        )
        assert extract_emails(snippet) == [
            "hello@acme.io",
            "support@acme.io",
            "press@acme.io",
        ]

    def test_deduplicates_case_insensitively(self):
        assert extract_emails("Hello@Acme.io and hello@acme.io") == ["hello@acme.io"]

    def test_strips_trailing_punctuation(self):
        assert extract_emails("write to hello@acme.io.") == ["hello@acme.io"]

    def test_filters_asset_noise_from_real_world_snippet(self):
        snippet = "<img src='logo@2x.png'> Email hello@acme.io — see you@example.com"
        assert extract_emails(snippet) == ["hello@acme.io"]

    def test_empty_input(self):
        assert extract_emails("") == []


class TestDomainMatchesProduct:
    @pytest.mark.parametrize(
        "domain,product",
        [
            ("toplify.app", "Toplify"),
            ("subtitlegenerator.app", "SubtitleGenerator"),
            ("maccess.io", "Maccess"),
            ("getlinear.com", "Linear"),
            ("linear.app", "Linear"),
        ],
    )
    def test_accepts_matching_domains(self, domain, product):
        assert domain_matches_product(domain, product)

    @pytest.mark.parametrize(
        "domain,product",
        [
            ("threads.com", "Toplify"),   # the real regression: louder namesake
            ("dev.to", "Open Analytics"), # blog host, not the company
            ("random.io", "Toplify"),
            ("", "Toplify"),
        ],
    )
    def test_rejects_unrelated_domains(self, domain, product):
        assert not domain_matches_product(domain, product)


class TestIsCompanyHost:
    @pytest.mark.parametrize(
        "url",
        ["https://acme.io/about", "https://www.toplify.app"],
    )
    def test_accepts_company_sites(self, url):
        assert is_company_host(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.producthunt.com/posts/toplify",
            "https://twitter.com/acme",
            "https://dev.to/someone/post",
            "https://github.com/acme/repo",
            "https://apps.apple.com/app/id123",
            "",
        ],
    )
    def test_rejects_aggregators_and_socials(self, url):
        assert not is_company_host(url)


class TestBestEmail:
    def test_prefers_the_company_domain(self):
        candidates = ["someone@gmail.com", "hello@acme.io"]
        assert best_email(candidates, "acme.io") == "hello@acme.io"

    def test_prefers_role_addresses_over_personal_ones(self):
        candidates = ["bob.smith@acme.io", "hello@acme.io"]
        assert best_email(candidates, "acme.io") == "hello@acme.io"

    def test_role_preference_order(self):
        assert best_email(["sales@acme.io", "hello@acme.io"], "acme.io") == "hello@acme.io"

    def test_accepts_subdomain_of_the_company(self):
        assert best_email(["hi@mail.acme.io"], "acme.io") == "hi@mail.acme.io"

    def test_strict_mode_drops_off_domain_addresses(self):
        # The Toplify regression: a real address belonging to someone else.
        assert best_email(["support@appps.od.ua"], "toplify.app") == ""

    def test_falls_back_to_product_name_when_no_domain_resolved(self):
        assert best_email(["hello@toplify.app"], "", product_name="Toplify") == (
            "hello@toplify.app"
        )

    def test_no_domain_and_no_name_match_yields_nothing(self):
        assert best_email(["support@appps.od.ua"], "", product_name="Toplify") == ""

    def test_non_strict_mode_allows_off_domain(self):
        assert (
            best_email(["support@appps.od.ua"], "toplify.app", strict=False)
            == "support@appps.od.ua"
        )

    def test_returns_empty_for_no_candidates(self):
        assert best_email([], "acme.io") == ""

    def test_returns_empty_when_every_candidate_is_junk(self):
        assert best_email(["you@example.com", "logo@2x.png"], "acme.io") == ""
