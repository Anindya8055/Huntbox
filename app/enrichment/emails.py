"""Email extraction, validation and ranking for Stage 2.

Serper snippets are noisy: they contain filenames that look like addresses
(``logo@2x.png``), Sentry DSNs, version strings and truncated text. These
helpers pull out email-shaped strings, throw away the junk, and pick the
candidate most likely to be a real contact address for a given company.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Deliberately stricter than RFC 5322: the TLD must be alphabetic and at
# least two characters, which kills most asset-filename false positives.
EMAIL_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._%+\-]{0,62}[A-Za-z0-9])?"
    r"@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,24}",
)

# Image/asset extensions that regularly appear after an "@" in snippets.
_ASSET_TLDS = {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "css", "js",
    "json", "xml", "pdf", "zip", "mp4", "webm", "woff", "woff2", "ttf",
}

# Addresses that are real but useless as a company contact.
_JUNK_LOCALPARTS = {
    "example", "email", "your", "youremail", "name", "user", "username",
    "someone", "test", "no-reply", "noreply", "donotreply", "do-not-reply",
    "sentry", "wixpress",
}

_JUNK_DOMAINS = {
    "example.com", "example.org", "example.net", "domain.com", "email.com",
    "yourdomain.com", "company.com", "sentry.io", "sentry.wixpress.com",
    "producthunt.com", "schema.org", "w3.org", "2x.png",
}

# Ordered best-first. A generic company inbox beats a random person's address.
_ROLE_PREFERENCE = (
    "hello", "hi", "contact", "support", "info", "team", "sales",
    "press", "help", "hey", "founders", "care", "admin",
)

# Hosts that are never the company's own site.
_NON_COMPANY_HOSTS = {
    "producthunt.com", "twitter.com", "x.com", "linkedin.com", "facebook.com",
    "instagram.com", "youtube.com", "tiktok.com", "reddit.com", "medium.com",
    "github.com", "gitlab.com", "crunchbase.com", "wikipedia.org", "g2.com",
    "capterra.com", "trustpilot.com", "glassdoor.com", "apps.apple.com",
    "play.google.com", "apple.com", "microsoft.com", "amazon.com",
    "wellfound.com", "angel.co", "indiehackers.com", "substack.com",
    "notion.site", "notion.so", "ycombinator.com", "slideshare.net",
    "pinterest.com", "quora.com", "bing.com", "google.com",
    "dev.to", "threads.com", "threads.net", "hashnode.dev", "wordpress.com",
    "blogspot.com", "stackoverflow.com", "producthunt.net", "alternativeto.net",
    "saashub.com", "toolify.ai", "futurepedia.io", "theresanaiforthat.com",
    "idcrawl.com", "webtoons.com", "seamless.ai",
    "sourceforge.net", "npmjs.com", "pypi.org", "softpedia.com", "cnet.com",
    "getapp.com", "softwareadvice.com", "appsumo.com", "gumroad.com",
}


def _slug(text: str) -> str:
    """Lowercase alphanumeric squash: 'Open Analytics' -> 'openanalytics'."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def domain_matches_product(domain: str, product_name: str) -> bool:
    """Does this domain plausibly belong to this product?

    Guards against the common failure where the top search result is a
    louder company with a similar name (Toplify -> threads.com) or a blog
    host that merely wrote about it (Open Analytics -> dev.to).
    """
    reg = registrable_domain(domain)
    if not reg:
        return False

    label = _slug(reg.split(".")[0])
    name = _slug(product_name)
    if not label or not name:
        return False

    # Exact, or one contains the other (getlinear.com vs linear).
    if label == name or name in label or label in name:
        return True

    # Multi-word names often become initials or a leading word:
    # "Open Analytics" -> openanalytics / oanalytics / analytics.
    words = [_slug(w) for w in re.split(r"\s+", product_name.strip()) if _slug(w)]
    if len(words) > 1:
        if label == "".join(w[0] for w in words):
            return True
        if any(len(w) >= 5 and (w == label or w in label) for w in words):
            return True
    return False


def normalize_domain(url_or_host: str) -> str:
    """Reduce a URL or host to a bare lowercase domain, minus ``www.``."""
    if not url_or_host:
        return ""
    value = url_or_host.strip()
    if "://" not in value:
        value = "http://" + value
    host = (urlparse(value).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def registrable_domain(domain: str) -> str:
    """Rough eTLD+1, good enough to compare ``mail.acme.com`` with ``acme.com``."""
    parts = [p for p in normalize_domain(domain).split(".") if p]
    if len(parts) < 2:
        return ""
    # Handle the common two-part public suffixes (.co.uk, .com.au, ...).
    if len(parts) >= 3 and parts[-2] in {"co", "com", "net", "org", "gov", "ac"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_company_host(url: str) -> bool:
    """True if a search-result link plausibly points at a company's own site."""
    domain = normalize_domain(url)
    if not domain or "." not in domain:
        return False
    reg = registrable_domain(domain)
    return reg not in _NON_COMPANY_HOSTS and domain not in _NON_COMPANY_HOSTS


def is_plausible_email(addr: str) -> bool:
    """Reject asset filenames, placeholders and known-junk addresses."""
    if not addr or addr.count("@") != 1:
        return False
    if not EMAIL_RE.fullmatch(addr):
        return False

    local, _, domain = addr.partition("@")
    domain = domain.lower()
    local_l = local.lower()

    if len(addr) > 254 or len(local) > 64:
        return False
    if ".." in addr:
        return False

    tld = domain.rsplit(".", 1)[-1]
    if tld in _ASSET_TLDS:
        return False
    if domain in _JUNK_DOMAINS or registrable_domain(domain) in _JUNK_DOMAINS:
        return False
    if local_l in _JUNK_LOCALPARTS:
        return False
    # Long hex blobs are Sentry keys and tracking ids, not people.
    if len(local) >= 24 and re.fullmatch(r"[0-9a-f]+", local_l):
        return False
    return True


def extract_emails(text: str) -> list[str]:
    """Pull every plausible, de-duplicated address out of a block of text."""
    if not text:
        return []
    seen: dict[str, None] = {}
    for raw in EMAIL_RE.findall(text):
        addr = raw.strip(" .,;:()<>[]\"'").lower()
        if is_plausible_email(addr) and addr not in seen:
            seen[addr] = None
    return list(seen)


def _score(addr: str, domain: str) -> tuple[int, int]:
    """Lower is better: (domain match tier, role preference index)."""
    email_domain = addr.rsplit("@", 1)[-1]
    target = registrable_domain(domain)
    email_reg = registrable_domain(email_domain)

    if target and email_reg == target:
        domain_tier = 0
    elif target and (email_reg.endswith("." + target) or target.endswith("." + email_reg)):
        domain_tier = 1
    elif email_reg in {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com"}:
        domain_tier = 3  # free inboxes are a last resort
    else:
        domain_tier = 2

    local = addr.split("@", 1)[0]
    try:
        role = _ROLE_PREFERENCE.index(local)
    except ValueError:
        role = len(_ROLE_PREFERENCE)
    return domain_tier, role


def best_email(
    candidates: list[str],
    domain: str = "",
    product_name: str = "",
    strict: bool = True,
) -> str:
    """Pick the most likely company contact address, or '' if there is none.

    With ``strict`` (the default) an address is only accepted when its own
    domain belongs to the company -- either matching the verified ``domain``
    or, when no domain was resolved, matching ``product_name``. An unrelated
    address is worse than no address, so those are dropped rather than
    returned with a caveat.
    """
    valid = [c for c in dict.fromkeys(candidates) if is_plausible_email(c)]
    if not valid:
        return ""

    if strict:
        target = registrable_domain(domain)
        kept = []
        for addr in valid:
            addr_reg = registrable_domain(addr.rsplit("@", 1)[-1])
            if target:
                if addr_reg == target or addr_reg.endswith("." + target):
                    kept.append(addr)
            elif product_name and domain_matches_product(addr_reg, product_name):
                kept.append(addr)
        valid = kept

    if not valid:
        return ""
    return min(valid, key=lambda a: _score(a, domain))
