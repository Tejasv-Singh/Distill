import asyncio

import pytest

from app.scraping.fetcher import Fetcher, FetchError, detect_bot_wall
from app.security import RateLimiter, UnsafeURL, normalize_url, resolve_public, safe_filename


@pytest.mark.parametrize("raw,expected", [
    ("example.com", "https://example.com/"),
    ("HTTPS://Example.COM/a?b=1#frag", "https://example.com/a?b=1"),
    ("http://example.com:8080/x", "http://example.com:8080/x"),
])
def test_normalize_ok(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "javascript:alert(1)", "file:///etc/passwd", "ftp://example.com/x", "gopher://x.com",
    "http://user:pass@example.com/", "http://exa mple.com", "notaurl", "http://example.com:99999/",
    "data:text/html,<b>x</b>", "x" * 3000,
])
def test_normalize_rejects(raw):
    with pytest.raises(UnsafeURL):
        normalize_url(raw)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost:9999/", "http://10.0.0.5/", "http://192.168.1.1/",
    "http://172.16.0.1/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
    "http://0.0.0.0/", "http://100.64.0.1/", "http://[::ffff:127.0.0.1]/", "http://2130706433/",
])
def test_private_addresses_blocked(url):
    with pytest.raises(UnsafeURL):
        asyncio.run(resolve_public(url))


def test_redirect_to_internal_is_blocked(demo_url):
    async def go():
        f = Fetcher()
        try:
            await f.fetch_static(demo_url + "/redirect-internal")
        finally:
            await f.close()
    with pytest.raises(FetchError) as e:
        asyncio.run(go())
    assert e.value.kind == "blocked_url"


def test_bot_wall_detected(demo_url):
    async def go():
        f = Fetcher()
        try:
            await f.fetch_static(demo_url + "/bot-wall")
        finally:
            await f.close()
    with pytest.raises(FetchError) as e:
        asyncio.run(go())
    assert e.value.kind == "bot_protection"
    assert detect_bot_wall(200, {"cf-mitigated": "challenge"}, "")
    assert not detect_bot_wall(200, {}, "<html><body>" + "Normal article text. " * 500 + "</body></html>")


def test_404_is_friendly(demo_url):
    async def go():
        f = Fetcher()
        try:
            await f.fetch_static(demo_url + "/nope.html")
        finally:
            await f.close()
    with pytest.raises(FetchError) as e:
        asyncio.run(go())
    assert e.value.kind == "not_found"


def test_rate_limiter():
    rl = RateLimiter(3)
    assert [rl.check("a") for _ in range(3)] == [None, None, None]
    assert rl.check("a") is not None
    assert rl.check("b") is None


def test_safe_filename():
    assert safe_filename("../../etc/passwd") == "etcpasswd"
    assert safe_filename("Q3 report: final?") == "Q3_report_final"
    assert safe_filename("") == "output"
