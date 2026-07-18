"""Tests for connect()/disconnect() resilience fixes ported from upstream PR #17469:

- scoped per-JID account lock (acquire on connect, release on disconnect)
- bounded wait for session establishment, with a fatal timeout instead of a
  premature "connected" report
- host/port are honored on both the primary and fallback connect() signatures
- TLS enforcement knobs (enable_starttls / enable_direct_tls / enable_plaintext)
  and XMPP_DIRECT_TLS / direct_tls config
- failed_all_auth (not failed_auth) drives a non-retryable fatal + notify
- unexpected disconnect escalates to a retryable fatal + notify; a deliberate
  disconnect() does not
- presence-subscribe auto-approval is gated by the allow-list

Run: python -m pytest tests/test_connect_resilience.py -v
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import unittest.mock

# -----------------------------------------------------------------
# Mock gateway / tools so adapter.py imports cleanly
# -----------------------------------------------------------------

gateway_mod = unittest.mock.MagicMock()
sys.modules["gateway"] = gateway_mod

gw_config = unittest.mock.MagicMock()
sys.modules["gateway.config"] = gw_config
gw_config.Platform = type("Platform", (), {"__init__": lambda self, *a, **k: None, "value": "xmpp"})
gw_config.PlatformConfig = type("PlatformConfig", (), {"__init__": lambda self, *a, **k: None})

gw_platforms = unittest.mock.MagicMock()
sys.modules["gateway.platforms"] = gw_platforms

gw_base = unittest.mock.MagicMock()
sys.modules["gateway.platforms.base"] = gw_base


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


gw_base.MessageEvent = _FakeMessageEvent
gw_base.MessageType = type("MessageType", (), {"TEXT": "text", "IMAGE": "image", "COMMAND": "command"})


class _FakeProcessingOutcome:
    SUCCESS = 0
    FAILURE = 1
    CANCELLED = 2


gw_base.ProcessingOutcome = _FakeProcessingOutcome
gw_base.SendResult = type("SendResult", (), {
    "__init__": lambda s, **kw: s.__dict__.update(kw) or None,
})

# -----------------------------------------------------------------
# A faithful-enough BasePlatformAdapter fake: real fatal-error/lock/notify
# bookkeeping so connect()/disconnect() can be exercised end-to-end, with
# hooks tests can flip (lock acquisition outcome, notify handler capture).
# -----------------------------------------------------------------

acquire_calls = []
release_calls = []
notify_calls = []
LOCK_SHOULD_SUCCEED = [True]


def _base_init(self, *a, **k):
    self.config = a[0] if a else None
    self._fatal_error_code = None
    self._fatal_error_message = None
    self._fatal_error_retryable = True
    self._fatal_error_handler = None
    self._platform_lock_identity = None
    self._platform_lock_scope = None


def _base_set_fatal_error(self, code, message, *, retryable):
    self._fatal_error_code = code
    self._fatal_error_message = message
    self._fatal_error_retryable = retryable


def _base_mark_connected(self):
    self._fatal_error_code = None
    self._fatal_error_message = None


def _base_mark_disconnected(self):
    pass


async def _base_notify_fatal_error(self):
    notify_calls.append(self)
    handler = getattr(self, "_fatal_error_handler", None)
    if not handler:
        return
    result = handler(self)
    if asyncio.iscoroutine(result):
        await result


def _base_acquire_platform_lock(self, scope, identity, resource_desc):
    acquire_calls.append((scope, identity, resource_desc))
    self._platform_lock_scope = scope
    if LOCK_SHOULD_SUCCEED[0]:
        self._platform_lock_identity = identity
        return True
    self._set_fatal_error(f"{scope}_lock", f"{resource_desc} already in use", retryable=False)
    return False


def _base_release_platform_lock(self):
    identity = getattr(self, "_platform_lock_identity", None)
    if identity:
        release_calls.append(identity)
    self._platform_lock_identity = None


gw_base.BasePlatformAdapter = type("BasePlatformAdapter", (), {
    "__init__": _base_init,
    "emit_message_raw": lambda *a, **kw: None,
    "on_processing_start": lambda *a, **kw: None,
    "on_processing_complete": lambda *a, **kw: None,
    "send": lambda *a, **kw: None,
    "handle_message": lambda *a, **kw: None,
    "build_source": lambda s, **kw: MagicMock(**kw),
    "_mark_disconnected": _base_mark_disconnected,
    "_mark_connected": _base_mark_connected,
    "_set_fatal_error": _base_set_fatal_error,
    "_notify_fatal_error": _base_notify_fatal_error,
    "_acquire_platform_lock": _base_acquire_platform_lock,
    "_release_platform_lock": _base_release_platform_lock,
    "has_fatal_error": property(lambda s: s._fatal_error_message is not None),
    "fatal_error_message": property(lambda s: s._fatal_error_message),
    "fatal_error_code": property(lambda s: s._fatal_error_code),
    "fatal_error_retryable": property(lambda s: s._fatal_error_retryable),
})

gw_models = unittest.mock.MagicMock()
sys.modules["gateway.platforms.models"] = gw_models
gw_models.ChatContext = type("ChatContext", (), {"__init__": lambda self, **kw: None})

gw_util = unittest.mock.MagicMock()
sys.modules["gateway.util"] = gw_util

tools_mod = unittest.mock.MagicMock()
sys.modules["tools"] = tools_mod
tools_gateway = unittest.mock.MagicMock()
sys.modules["tools.clarify_gateway"] = tools_gateway
tools_gateway.mark_awaiting_text = unittest.mock.MagicMock()

# slixmpp-omemo / omemo are left as real imports (installed in this env) but
# every test below disables OMEMO via config to keep the fake client simple.

if "adapter" in sys.modules:
    del sys.modules["adapter"]
for key in list(sys.modules.keys()):
    if key.startswith("adapter."):
        del sys.modules[key]

import adapter  # noqa: E402


# -----------------------------------------------------------------
# Fake slixmpp client — enough surface for connect()/disconnect()
# -----------------------------------------------------------------

class _FakeRoster:
    auto_authorize = True
    auto_subscribe = True


class _FakeSlixmppClient:
    def __init__(self, jid, password):
        self.jid = jid
        self.password = password
        self.boundjid = MagicMock(bare=jid)
        self.plugins = {}
        self.roster = _FakeRoster()
        self.handlers = {}
        self.disconnected = asyncio.get_event_loop().create_future()
        self.connect_calls = []
        self.enable_starttls = None
        self.enable_direct_tls = None
        self.enable_plaintext = None

    def register_plugin(self, name, *a, **k):
        self.plugins[name] = MagicMock()

    def add_event_handler(self, name, handler):
        self.handlers.setdefault(name, []).append(handler)

    def connect(self, *a, **k):
        self.connect_calls.append((a, k))
        return None

    def disconnect(self, *a, **k):
        if not self.disconnected.done():
            self.disconnected.set_result(None)

    def send_presence(self, *a, **k):
        pass

    async def get_roster(self):
        pass

    def __getitem__(self, k):
        return self.plugins.get(k)

    def __contains__(self, k):
        return k in self.plugins

    async def fire(self, event_name, arg=None):
        for handler in self.handlers.get(event_name, []):
            result = handler(arg)
            if asyncio.iscoroutine(result):
                await result


def _make_cfg(**extra):
    cfg = MagicMock()
    base = {"jid": "hermes@example.org", "password": "secret", "omemo_enabled": "false"}
    base.update(extra)
    cfg.extra = base
    return cfg


@pytest.fixture(autouse=True)
def _reset_state():
    acquire_calls.clear()
    release_calls.clear()
    notify_calls.clear()
    LOCK_SHOULD_SUCCEED[0] = True
    yield


# -----------------------------------------------------------------
# Scoped per-JID lock
# -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_acquires_lock_on_normalized_bare_jid(monkeypatch):
    a = adapter.XmppAdapter(_make_cfg(jid="Hermes@Example.ORG/ignored-resource"))
    monkeypatch.setattr(adapter, "ClientXMPP", _FakeSlixmppClient)
    a._CONNECT_TIMEOUT_SECS = 0.2

    async def _drive():
        await asyncio.sleep(0.01)
        await a.client.fire("session_start")

    task = asyncio.create_task(_drive())
    ok = await a.connect()
    await task
    assert ok is True
    assert acquire_calls == [("xmpp", "hermes@example.org", "XMPP account hermes@example.org")]


@pytest.mark.asyncio
async def test_connect_fails_and_returns_false_when_lock_held(monkeypatch):
    LOCK_SHOULD_SUCCEED[0] = False
    a = adapter.XmppAdapter(_make_cfg())
    monkeypatch.setattr(adapter, "ClientXMPP", _FakeSlixmppClient)

    ok = await a.connect()
    assert ok is False
    assert a.client is None
    assert a._platform_lock_identity is None
    assert a.has_fatal_error is True


@pytest.mark.asyncio
async def test_disconnect_releases_lock(monkeypatch):
    a = adapter.XmppAdapter(_make_cfg())
    monkeypatch.setattr(adapter, "ClientXMPP", _FakeSlixmppClient)
    a._CONNECT_TIMEOUT_SECS = 0.2

    async def _drive():
        await asyncio.sleep(0.01)
        await a.client.fire("session_start")

    task = asyncio.create_task(_drive())
    ok = await a.connect()
    await task
    assert ok is True

    await a.disconnect()
    assert release_calls == ["hermes@example.org"]


@pytest.mark.asyncio
async def test_standalone_sender_skips_lock(monkeypatch):
    monkeypatch.setattr(adapter, "ClientXMPP", _FakeSlixmppClient)
    cfg = _make_cfg()

    async def _fake_send(*a, **kw):
        return adapter.SendResult(success=True, message_id="m1")

    a = None
    orig_init = adapter.XmppAdapter.__init__

    def _capture_init(self, *args, **kwargs):
        nonlocal a
        orig_init(self, *args, **kwargs)
        a = self

    monkeypatch.setattr(adapter.XmppAdapter, "__init__", _capture_init)
    monkeypatch.setattr(adapter.XmppAdapter, "send", _fake_send)

    async def _drive():
        while a is None or a.client is None:
            await asyncio.sleep(0.005)
        await a.client.fire("session_start")

    driver = asyncio.create_task(_drive())
    result = await adapter.send_xmpp_message(cfg, "user@example.org", "hi")
    await driver

    assert result["success"] is True
    assert acquire_calls == []
    assert release_calls == []


# -----------------------------------------------------------------
# Bounded session-establishment wait
# -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_times_out_when_session_never_starts(monkeypatch):
    a = adapter.XmppAdapter(_make_cfg())
    monkeypatch.setattr(adapter, "ClientXMPP", _FakeSlixmppClient)
    a._CONNECT_TIMEOUT_SECS = 0.05

    ok = await a.connect()
    assert ok is False
    assert a.fatal_error_code == "xmpp_connect_timeout"
    assert a.client is None  # disconnect() clears it
    assert release_calls == ["hermes@example.org"]  # failed connect still releases the lock


@pytest.mark.asyncio
async def test_failed_all_auth_sets_nonretryable_fatal_and_notifies():
    a = adapter.XmppAdapter(_make_cfg())
    await a._on_failed_all_auth(None)
    assert a.fatal_error_code == "xmpp_auth_failed"
    assert a.fatal_error_retryable is False
    assert notify_calls == [a]


@pytest.mark.asyncio
async def test_connect_returns_false_when_auth_fails_during_wait(monkeypatch):
    a = adapter.XmppAdapter(_make_cfg())
    monkeypatch.setattr(adapter, "ClientXMPP", _FakeSlixmppClient)
    a._CONNECT_TIMEOUT_SECS = 0.2

    async def _drive():
        await asyncio.sleep(0.01)
        await a.client.fire("failed_all_auth")

    task = asyncio.create_task(_drive())
    ok = await a.connect()
    await task
    assert ok is False
    assert a.fatal_error_code == "xmpp_auth_failed"
    assert a.fatal_error_retryable is False


# -----------------------------------------------------------------
# host/port passthrough
# -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_passes_host_and_port(monkeypatch):
    a = adapter.XmppAdapter(_make_cfg(host="xmpp.example.net", port=5269))
    monkeypatch.setattr(adapter, "ClientXMPP", _FakeSlixmppClient)
    a._CONNECT_TIMEOUT_SECS = 0.2

    async def _drive():
        await asyncio.sleep(0.01)
        await a.client.fire("session_start")

    task = asyncio.create_task(_drive())
    ok = await a.connect()
    await task
    assert ok is True
    assert a.client.connect_calls == [((), {"host": "xmpp.example.net", "port": 5269})]


# -----------------------------------------------------------------
# TLS enforcement + direct TLS
# -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_sets_tls_enforcement_knobs(monkeypatch):
    a = adapter.XmppAdapter(_make_cfg())
    monkeypatch.setattr(adapter, "ClientXMPP", _FakeSlixmppClient)
    a._CONNECT_TIMEOUT_SECS = 0.2

    async def _drive():
        await asyncio.sleep(0.01)
        await a.client.fire("session_start")

    task = asyncio.create_task(_drive())
    await a.connect()
    await task
    assert a.client.enable_starttls is True
    assert a.client.enable_plaintext is False
    assert a.client.enable_direct_tls is False  # port 5222 default


def test_direct_tls_defaults_off_on_standard_port():
    a = adapter.XmppAdapter(_make_cfg(port=5222))
    assert a.direct_tls is False


def test_direct_tls_auto_on_for_port_5223():
    a = adapter.XmppAdapter(_make_cfg(port=5223))
    assert a.direct_tls is True


def test_direct_tls_extra_key_forces_on():
    a = adapter.XmppAdapter(_make_cfg(port=5222, direct_tls="true"))
    assert a.direct_tls is True


def test_direct_tls_extra_key_forces_off_beats_port():
    a = adapter.XmppAdapter(_make_cfg(port=5223, direct_tls="false"))
    assert a.direct_tls is False


# -----------------------------------------------------------------
# Disconnect escalation semantics
# -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_deliberate_disconnect_does_not_escalate(monkeypatch):
    a = adapter.XmppAdapter(_make_cfg())
    monkeypatch.setattr(adapter, "ClientXMPP", _FakeSlixmppClient)
    a._CONNECT_TIMEOUT_SECS = 0.2

    async def _drive():
        await asyncio.sleep(0.01)
        await a.client.fire("session_start")

    task = asyncio.create_task(_drive())
    await a.connect()
    await task

    await a.disconnect()
    assert a.has_fatal_error is False
    assert notify_calls == []


@pytest.mark.asyncio
async def test_unexpected_disconnect_escalates_and_notifies(monkeypatch):
    a = adapter.XmppAdapter(_make_cfg())
    monkeypatch.setattr(adapter, "ClientXMPP", _FakeSlixmppClient)
    a._CONNECT_TIMEOUT_SECS = 0.2

    async def _drive():
        await asyncio.sleep(0.01)
        await a.client.fire("session_start")

    task = asyncio.create_task(_drive())
    await a.connect()
    await task

    # Server drops the connection out of the blue (no disconnect() call).
    await a._on_disconnected(None)
    assert a.fatal_error_code == "xmpp_connection_lost"
    assert a.fatal_error_retryable is True
    assert notify_calls == [a]


# -----------------------------------------------------------------
# Presence-subscribe gating
# -----------------------------------------------------------------

class _FakePresence:
    def __init__(self, from_jid):
        self._from_jid = from_jid

    def get_from(self):
        return self._from_jid


@pytest.mark.asyncio
async def test_subscribe_approved_for_allowed_peer():
    a = adapter.XmppAdapter(_make_cfg(allowed_users="alice@example.org"))
    client = MagicMock()
    a.client = client
    await a._on_subscribe(_FakePresence("alice@example.org/phone"))
    client.send_presence.assert_any_call(pto="alice@example.org", ptype="subscribed")
    client.send_presence.assert_any_call(pto="alice@example.org", ptype="subscribe")


@pytest.mark.asyncio
async def test_subscribe_ignored_for_disallowed_peer():
    a = adapter.XmppAdapter(_make_cfg(allowed_users="alice@example.org"))
    client = MagicMock()
    a.client = client
    await a._on_subscribe(_FakePresence("mallory@evil.example/x"))
    client.send_presence.assert_not_called()


# -----------------------------------------------------------------
# splits_long_messages capability flag
# -----------------------------------------------------------------

def test_splits_long_messages_declared():
    a = adapter.XmppAdapter(_make_cfg())
    assert a.splits_long_messages is True
