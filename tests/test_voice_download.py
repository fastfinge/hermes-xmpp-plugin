"""Real HTTPX streaming regressions; no network or live configuration."""
import asyncio
import sys

import httpx
import pytest

from test_voice_notes import adapter, core


@pytest.fixture
def http_transport(core, monkeypatch):
    def install(handler):
        monkeypatch.setattr(sys.modules["tools.url_safety"], "create_ssrf_safe_async_client",
                            lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw))
    return install


def test_redirect_body_is_not_buffered(core, http_transport):
    consumed = []
    core.max_bytes = 8

    class RedirectBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(6):
                consumed.append(8)
                yield b"x" * 8

    def serve(request):
        if request.url.path == "/start.ogg":
            return httpx.Response(302, headers={"location": "https://public.example/final.ogg"},
                                  stream=RedirectBody())
        return httpx.Response(200, content=b"OggS")

    http_transport(serve)
    assert asyncio.run(adapter._fetch_voice_bytes("https://public.example/start.ogg", trusted=False)) == b"OggS"
    assert consumed == [], "HTTPX buffered an uncapped intermediate response"


def test_public_redirect_cannot_downgrade_https(core, http_transport):
    seen = []

    def serve(request):
        seen.append(str(request.url))
        if request.url.path == "/start.ogg":
            return httpx.Response(302, headers={"location": "http://public.example/final.ogg"})
        return httpx.Response(200, content=b"OggS")

    http_transport(serve)
    with pytest.raises(ValueError, match="HTTPS"):
        asyncio.run(adapter._fetch_voice_bytes("https://public.example/start.ogg", trusted=False))
    assert seen == ["https://public.example/start.ogg"]


def test_dns_safety_checks_do_not_block_event_loop(core, http_transport, monkeypatch):
    import threading

    loop_thread = threading.get_ident()
    checked_threads = []

    def safe(url):
        checked_threads.append(threading.get_ident())
        return True

    def floor(url):
        checked_threads.append(threading.get_ident())
        return False

    core.safe = safe
    monkeypatch.setattr(sys.modules["tools.url_safety"], "is_always_blocked_url", floor)

    def serve(request):
        if request.url.path == "/start.ogg":
            return httpx.Response(302, headers={"location": "https://public.example/final.ogg"})
        return httpx.Response(200, content=b"OggS")

    http_transport(serve)
    assert asyncio.run(adapter._fetch_voice_bytes("https://public.example/start.ogg", trusted=False)) == b"OggS"
    assert checked_threads and all(thread != loop_thread for thread in checked_threads)


@pytest.mark.parametrize("declared", [None, "1"])
def test_stream_cap_ignores_missing_or_false_content_length(core, http_transport, declared):
    core.max_bytes = 8

    class AudioBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"OggS"
            yield b"x" * 8

    def serve(request):
        headers = {} if declared is None else {"content-length": declared}
        return httpx.Response(200, headers=headers, stream=AudioBody())

    http_transport(serve)
    with pytest.raises(ValueError, match="too large"):
        asyncio.run(adapter._fetch_voice_bytes("https://public.example/a.ogg", trusted=False))


@pytest.mark.parametrize("target", ["https://private.example/a.ogg", "https://169.254.169.254/a.ogg"])
def test_redirect_rechecks_address_policy_before_request(core, http_transport, target):
    seen = []
    core.safe = lambda url: "private.example" not in url

    def serve(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": target})

    http_transport(serve)
    with pytest.raises(ValueError):
        asyncio.run(adapter._fetch_voice_bytes("https://public.example/start.ogg", trusted=False))
    assert seen == ["https://public.example/start.ogg"]


def test_redirect_loop_is_bounded(core, http_transport):
    seen = []

    def serve(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "/start.ogg"})

    http_transport(serve)
    with pytest.raises(ValueError, match="too many"):
        asyncio.run(adapter._fetch_voice_bytes("https://public.example/start.ogg", trusted=False))
    assert len(seen) == 21
