"""Inbound voice notes → Hermes STT.

A message that is exactly one audio attachment and no text must reach the
gateway as a VOICE MessageEvent carrying the locally cached audio path in
``media_urls`` and an ``audio/*`` entry in ``media_types`` — the shape Hermes
core's STT pipeline transcribes with the operator's configured provider.

Detection runs against REAL slixmpp stanzas (parsed from XML) so the OOB /
SFS element paths are exercised exactly as clients send them.

Run: python -m pytest tests/test_voice_notes.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from conftest import evict_adapter_modules, restore_sys_modules, snapshot_sys_modules

_MODULES_SNAPSHOT = snapshot_sys_modules()
evict_adapter_modules()  # drop cached adapter/gateway copies BEFORE installing our fakes

# -----------------------------------------------------------------
# Minimal gateway fakes (same discipline as the other test files)
# -----------------------------------------------------------------
for name in ("gateway", "gateway.config", "gateway.platforms", "gateway.platforms.base",
             "gateway.platforms.models", "gateway.util", "tools", "tools.clarify_gateway"):
    sys.modules[name] = MagicMock()

gw_config = sys.modules["gateway.config"]
gw_config.Platform = type("Platform", (), {"__init__": lambda self, *a, **k: None})
gw_config.PlatformConfig = type("PlatformConfig", (), {"__init__": lambda self, *a, **k: None})


class _FakeMessageEvent:
    def __init__(self, *, text="", message_type=None, source=None, raw_message=None,
                 message_id=None, reply_to_message_id=None, reply_to_text=None, metadata=None):
        self.text = text
        self.message_type = message_type
        self.source = source
        self.raw_message = raw_message
        self.message_id = message_id
        self.reply_to_message_id = reply_to_message_id
        self.reply_to_text = reply_to_text
        self.metadata = metadata or {}
        self.media_urls = []
        self.media_types = []


captured = []


def _base_init(self, *a, **k):
    self.config = a[0] if a else None


async def _capture(self, event):
    captured.append(event)


gw_base = sys.modules["gateway.platforms.base"]
gw_base.MessageEvent = _FakeMessageEvent
gw_base.MessageType = type("MessageType", (), {"TEXT": "text", "VOICE": "voice", "AUDIO": "audio"})
gw_base.ProcessingOutcome = type("ProcessingOutcome", (), {"SUCCESS": 0, "FAILURE": 1, "CANCELLED": 2})
gw_base.SendResult = type("SendResult", (), {"__init__": lambda s, **kw: s.__dict__.update(kw) or None})
gw_base.BasePlatformAdapter = type("BasePlatformAdapter", (), {
    "__init__": _base_init,
    "handle_message": _capture,
    "build_source": lambda s, **kw: MagicMock(**kw),
})
sys.modules["gateway.platforms.models"].ChatContext = type("ChatContext", (), {"__init__": lambda s, **k: None})

import adapter  # noqa: E402

restore_sys_modules(_MODULES_SNAPSHOT)

from slixmpp import ClientXMPP  # noqa: E402
from slixmpp.stanza import Message  # noqa: E402
from slixmpp.xmlstream import ET  # noqa: E402

_XMPP = ClientXMPP("hermes@example.org/test", "x")
for _p in ("xep_0066", "xep_0446", "xep_0447", "xep_0300"):
    _XMPP.register_plugin(_p)

UP = "https://upload.example.org/abc"


def msg(inner: str, *, frm: str = "user@example.org/phone", mtype: str = "chat",
        mid: str = "m1") -> Message:
    xml = (f"<message xmlns='jabber:client' from='{frm}' to='hermes@example.org' "
           f"type='{mtype}' id='{mid}'>{inner}</message>")
    return Message(_XMPP, ET.fromstring(xml))


def oob(url: str) -> str:
    return f"<x xmlns='jabber:x:oob'><url>{url}</url></x>"


def sfs(url: str, media_type: str = "audio/ogg") -> str:
    return (
        "<file-sharing xmlns='urn:xmpp:sfs:0' disposition='inline'>"
        f"<file xmlns='urn:xmpp:file:metadata:0'><media-type>{media_type}</media-type>"
        "<name>voice</name><size>1234</size></file>"
        f"<sources><url-data xmlns='http://jabber.org/protocol/url-data' target='{url}'/></sources>"
        "</file-sharing>"
    )


def body(text: str) -> str:
    return f"<body>{text}</body>"


# -----------------------------------------------------------------
# Detection
# -----------------------------------------------------------------

def test_oob_voice_note_with_url_fallback_body_is_detected():
    url = f"{UP}/voice.m4a"
    v = adapter._extract_voice_attachment(msg(body(url) + oob(url)), url)
    assert v is not None
    assert (v.url, v.mime, v.ext, v.encrypted) == (url, "audio/mp4", ".m4a", False)


def test_sfs_voice_note_without_body_is_detected():
    url = f"{UP}/rec"  # no extension: type comes from SFS media-type
    v = adapter._extract_voice_attachment(msg(sfs(url, "audio/ogg; codecs=opus")), "")
    assert v is not None and v.mime == "audio/ogg" and v.ext == ".ogg"


def test_oob_and_sfs_advertising_same_url_count_as_one():
    url = f"{UP}/v.opus"
    v = adapter._extract_voice_attachment(msg(body(url) + oob(url) + sfs(url)), url)
    assert v is not None and v.url == url


def test_omemo_aesgcm_body_is_detected_as_encrypted():
    url = "aesgcm://upload.example.org/abc/voice.m4a#" + "00" * 12 + "11" * 32
    v = adapter._extract_voice_attachment(msg(body(url)), url)
    assert v is not None and v.encrypted and v.mime == "audio/mp4"


@pytest.mark.parametrize("inner,text", [
    (body("listen to this") + oob(f"{UP}/a.ogg"), "listen to this"),         # captioned
    (body(f"{UP}/a.jpg") + oob(f"{UP}/a.jpg"), f"{UP}/a.jpg"),               # not audio
    (oob(f"{UP}/a.ogg") + oob(f"{UP}/b.ogg"), ""),                          # two files
    (body("http://x.org/a.ogg") + oob("http://x.org/a.ogg"), "http://x.org/a.ogg"),  # plain http
    (body(f"{UP}/a.ogg"), f"{UP}/a.ogg"),                                   # bare link, no OOB
    (sfs(f"{UP}/a.ogg", "image/png"), ""),                                  # declared non-audio
    (body("hello"), "hello"),                                               # plain text
])
def test_non_voice_messages_are_not_detected(inner, text):
    assert adapter._extract_voice_attachment(msg(inner), text) is None


def test_detection_does_not_mutate_plain_stanzas():
    m = msg(body("hi"))
    adapter._extract_voice_attachment(m, "hi")
    assert m.xml.find("{jabber:x:oob}x") is None


# -----------------------------------------------------------------
# Trusted upload hosts (LAN / tailnet self-hosting)
# -----------------------------------------------------------------

def test_trusted_hosts_default_to_bot_domain_and_subdomains():
    pats = adapter._parse_trusted_hosts(None, "hermes@primushome.example/res")
    assert pats == ("primushome.example", "*.primushome.example")
    assert adapter._host_is_trusted("primushome.example", pats)
    assert adapter._host_is_trusted("upload.primushome.example", pats)
    assert adapter._host_is_trusted("UPLOAD.primushome.example.", pats)
    assert not adapter._host_is_trusted("evilprimushome.example", pats)
    assert not adapter._host_is_trusted("primushome.example.evil.org", pats)
    assert not adapter._host_is_trusted("", pats)


@pytest.mark.parametrize("raw,expected", [
    ("none", ()), ("off", ()), ("", ()),
    ("upload.x.org, 100.101.102.103 ,*.tail1.ts.net", ("upload.x.org", "100.101.102.103", "*.tail1.ts.net")),
    (["[fd7a:115c:a1e0::1]", "Upload.X.org."], ("fd7a:115c:a1e0::1", "upload.x.org")),
])
def test_trusted_hosts_explicit_values(raw, expected):
    assert adapter._parse_trusted_hosts(raw, "hermes@example.org") == expected


def test_tailnet_ip_literal_matches_exactly():
    pats = adapter._parse_trusted_hosts("100.101.102.103", "")
    assert adapter._host_is_trusted("100.101.102.103", pats)
    assert not adapter._host_is_trusted("100.101.102.104", pats)


# -----------------------------------------------------------------
# Fake Hermes core network/cache layer
# -----------------------------------------------------------------

class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.headers = {"content-length": str(len(payload))}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    async def aiter_bytes(self):
        half = len(self._payload) // 2
        yield self._payload[:half]
        yield self._payload[half:]


class _Client:
    """Serves ``routes[url] = (status, bytes)``; records how it was built/used."""

    def __init__(self, kind, routes, log, **kw):
        self.kind, self.routes, self.log = kind, routes, log
        self.max_redirects = 20
        log.append(("client", kind, kw))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url):
        self.log.append(("get", self.kind, url))
        status, payload = self.routes.get(url, (404, b""))
        resp = _Resp(status, payload)

        class _CM:
            async def __aenter__(self_inner):
                return resp

            async def __aexit__(self_inner, *a):
                return False

        return _CM()


@pytest.fixture
def core(monkeypatch):
    """Install fake ``tools.url_safety`` + ``gateway.platforms.base`` network bits."""
    state = types.SimpleNamespace(routes={}, log=[], cached=[], safe=lambda u: True,
                                  max_bytes=1 << 20)

    url_safety = types.ModuleType("tools.url_safety")
    url_safety.is_safe_url = lambda u: state.safe(u)
    url_safety.is_always_blocked_url = lambda u: "169.254.169.254" in u
    url_safety.create_ssrf_safe_async_client = (
        lambda **kw: _Client("ssrf_safe", state.routes, state.log, **kw))

    base = types.ModuleType("gateway.platforms.base")
    base.get_inbound_media_max_bytes = lambda: state.max_bytes

    def _validate(n, *, media_type="media", max_bytes=None):
        if max_bytes and n > max_bytes:
            raise ValueError(f"Inbound {media_type} payload is too large")
    base.validate_inbound_media_size = _validate

    async def _cache(data, ext):
        state.cached.append((data, ext))
        return f"/hermes/cache/audio/audio_{len(state.cached)}{ext}"
    base.cache_audio_from_bytes_async = _cache

    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.url_safety", url_safety)
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", base)
    monkeypatch.setattr(adapter, "_open_direct_client",
                        lambda **kw: _Client("direct", state.routes, state.log, **kw))
    return state


def _aesgcm(plaintext, iv_len=12):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv, key = os.urandom(iv_len), os.urandom(32)
    return AESGCM(key).encrypt(iv, plaintext, None), (iv + key).hex()


def test_parse_aesgcm_url_supports_12_and_16_byte_ivs():
    for iv_len in (12, 16):
        url = "aesgcm://h.example/p/v.ogg?x=1#" + "ab" * iv_len + "cd" * 32
        https_url, iv, key = adapter._parse_aesgcm_url(url)
        assert https_url == "https://h.example/p/v.ogg?x=1"
        assert len(iv) == iv_len and len(key) == 32
    with pytest.raises(ValueError):
        adapter._parse_aesgcm_url("aesgcm://h.example/v.ogg#abcd")


# -----------------------------------------------------------------
# _on_message end-to-end
# -----------------------------------------------------------------

def _adapter(**extra):
    cfg = MagicMock()
    cfg.extra = {"jid": "hermes@example.org", "password": "test-only", **extra}
    a = adapter.XmppAdapter(cfg)
    a.allow_all_users = True
    a._self_bare = "hermes@example.org"
    a.client = None  # skip XEP-0461 reply extraction; not under test here
    return a


@pytest.fixture
def xmpp():
    captured.clear()
    yield _adapter()
    captured.clear()


def test_public_host_voice_note_uses_ssrf_safe_client(xmpp, core):
    url = "https://files.public.example/voice.m4a"
    core.routes[url] = (200, b"\x00\x00\x00\x18ftypM4A ")
    asyncio.run(xmpp._on_message(msg(body(url) + oob(url), mid="v1")))
    kinds = [e[1] for e in core.log if e[0] == "get"]
    assert kinds == ["ssrf_safe"]
    client_kw = next(e[2] for e in core.log if e[0] == "client")
    assert client_kw["follow_redirects"] is False
    assert client_kw["trust_env"] is False
    assert core.cached == [(b"\x00\x00\x00\x18ftypM4A ", ".m4a")]
    evt = captured[0]
    assert evt.message_type == "voice" and evt.text == ""
    assert evt.media_urls == ["/hermes/cache/audio/audio_1.m4a"]
    assert evt.media_types == ["audio/mp4"]
    assert evt.message_id == "v1"
    assert evt.source.chat_id == "user@example.org"


def test_own_upload_host_on_tailnet_is_fetched_directly(xmpp, core):
    # SSRF policy would refuse it (resolves to 100.x / LAN) — trusted anyway.
    core.safe = lambda u: False
    url = "https://upload.example.org:5281/file_share/abc/voice.ogg"
    core.routes[url] = (200, b"OggS-voice")
    asyncio.run(xmpp._on_message(msg(body(url) + oob(url))))
    assert [e[1] for e in core.log if e[0] == "get"] == ["direct"]
    client_kw = next(e[2] for e in core.log if e[0] == "client")
    assert client_kw["follow_redirects"] is False
    assert captured[0].message_type == "voice"


def test_untrusted_private_host_is_refused_and_falls_back(xmpp, core, caplog):
    core.safe = lambda u: False
    url = "https://nas.lan.other/voice.ogg"
    core.routes[url] = (200, b"OggS")
    with caplog.at_level("WARNING"):
        asyncio.run(xmpp._on_message(msg(body(url) + oob(url))))
    assert not [e for e in core.log if e[0] == "get"]
    assert captured[0].message_type == "text" and captured[0].text == url
    assert "voice_trusted_hosts" in caplog.text and "nas.lan.other" in caplog.text


def test_trusted_host_redirect_is_not_followed(xmpp, core):
    url = "https://upload.example.org/voice.ogg"
    core.routes[url] = (302, b"")
    asyncio.run(xmpp._on_message(msg(body(url) + oob(url))))
    assert core.cached == []
    assert captured[0].message_type == "text"


def test_metadata_floor_applies_even_to_trusted_hosts(core):
    a = _adapter(voice_trusted_hosts="169.254.169.254")
    url = "https://169.254.169.254/latest/meta-data/x.ogg"
    core.routes[url] = (200, b"secret")
    captured.clear()
    asyncio.run(a._on_message(msg(body(url) + oob(url))))
    assert not [e for e in core.log if e[0] == "get"]
    assert captured[0].message_type == "text"


def test_size_cap_is_enforced(xmpp, core):
    core.max_bytes = 8
    url = "https://upload.example.org/voice.ogg"
    core.routes[url] = (200, b"OggS" + b"x" * 64)
    asyncio.run(xmpp._on_message(msg(body(url) + oob(url))))
    assert core.cached == []
    assert captured[0].message_type == "text"


def test_trust_none_sends_own_host_through_ssrf_policy(core):
    core.safe = lambda u: False
    a = _adapter(voice_trusted_hosts="none")
    url = "https://upload.example.org/voice.ogg"
    core.routes[url] = (200, b"OggS")
    captured.clear()
    asyncio.run(a._on_message(msg(body(url) + oob(url))))
    assert not [e for e in core.log if e[0] == "get"]
    assert captured[0].message_type == "text"


@pytest.mark.parametrize("iv_len", [12, 16])
def test_omemo_voice_note_is_decrypted_before_caching(xmpp, core, iv_len):
    plaintext = b"OggS" + os.urandom(48)
    ciphertext, frag = _aesgcm(plaintext, iv_len)
    https_url = "https://upload.example.org/abc/voice.ogg"
    core.routes[https_url] = (200, ciphertext)
    url = "aesgcm://upload.example.org/abc/voice.ogg#" + frag
    asyncio.run(xmpp._on_message(msg(body(url))))
    # Only the https form is requested: the key fragment never leaves the process.
    assert [e[2] for e in core.log if e[0] == "get"] == [https_url]
    assert core.cached == [(plaintext, ".ogg")]
    assert captured[0].message_type == "voice"


def test_omemo_tampered_ciphertext_is_dropped(xmpp, core):
    ciphertext, frag = _aesgcm(b"OggS-real")
    https_url = "https://upload.example.org/v.ogg"
    core.routes[https_url] = (200, ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]))
    url = "aesgcm://upload.example.org/v.ogg#" + frag
    asyncio.run(xmpp._on_message(msg(body(url))))
    assert core.cached == []
    assert captured == []


def test_unauthorized_sender_never_triggers_a_download(xmpp, core):
    xmpp.allow_all_users = False
    xmpp.allowed_users = set()
    url = "https://upload.example.org/voice.ogg"
    core.routes[url] = (200, b"OggS")
    asyncio.run(xmpp._on_message(msg(body(url) + oob(url), frm="stranger@evil.example/x")))
    assert core.log == []
    assert captured == []


def test_download_failure_without_body_drops_quietly(xmpp, core):
    asyncio.run(xmpp._on_message(msg(sfs("https://upload.example.org/missing.ogg"))))
    assert captured == []


def test_old_hermes_core_without_helpers_falls_back(xmpp, monkeypatch):
    async def _missing(url, *, trusted):
        raise ImportError("cannot import name 'is_always_blocked_url'")

    monkeypatch.setattr(adapter, "_fetch_voice_bytes", _missing)
    url = "https://upload.example.org/voice.ogg"
    asyncio.run(xmpp._on_message(msg(body(url) + oob(url))))
    assert captured[0].message_type == "text"


def test_captioned_audio_keeps_text_behaviour(xmpp, core):
    asyncio.run(xmpp._on_message(msg(body("what is this song?") + oob("https://upload.example.org/a.ogg"))))
    assert core.log == []
    assert captured[0].message_type == "text"
    assert captured[0].text == "what is this song?"


def test_toggle_off_restores_plain_url_text(core):
    a = _adapter(voice_transcription=False)
    captured.clear()
    url = "https://upload.example.org/voice.ogg"
    asyncio.run(a._on_message(msg(body(url) + oob(url))))
    assert core.log == []
    assert captured[0].message_type == "text" and captured[0].text == url


def test_toggle_defaults_on_and_reads_env(monkeypatch):
    monkeypatch.delenv("XMPP_VOICE_TRANSCRIPTION", raising=False)
    assert _adapter()._voice_transcription_enabled is True
    monkeypatch.setenv("XMPP_VOICE_TRANSCRIPTION", "false")
    assert _adapter()._voice_transcription_enabled is False


def test_trusted_hosts_read_from_env(monkeypatch):
    monkeypatch.setenv("XMPP_VOICE_TRUSTED_HOSTS", "prosody.tail1.ts.net")
    assert _adapter()._voice_trusted_hosts == ("prosody.tail1.ts.net",)


def test_yaml_config_carries_voice_keys_into_extra():
    extra = adapter._apply_yaml_config({}, {"voice_transcription": False,
                                            "voice_trusted_hosts": ["upload.x.org"]})
    assert extra["voice_transcription"] is False
    assert extra["voice_trusted_hosts"] == ["upload.x.org"]
