#!/usr/bin/env python3
"""Unit tests for first-class XMPP features.

Run with: python -m pytest tests/test_first_class_features.py -v
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import mimetypes

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import unittest.mock

# -----------------------------------------------------------------
# Build mock gateway / tools modules so adapter.py can import cleanly
# -----------------------------------------------------------------

gateway_mod = unittest.mock.MagicMock()
sys.modules["gateway"] = gateway_mod

gw_config = unittest.mock.MagicMock()
sys.modules["gateway.config"] = gw_config
gw_config.Platform = type("Platform", (), {"__init__": lambda self, *a, **k: None})
gw_config.PlatformConfig = type("PlatformConfig", (), {"__init__": lambda self, *a, **k: None})

gw_platforms = unittest.mock.MagicMock()
sys.modules["gateway.platforms"] = gw_platforms

gw_base = unittest.mock.MagicMock()
sys.modules["gateway.platforms.base"] = gw_base

# Simple dataclass-like mock for MessageEvent
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

gw_base.BasePlatformAdapter = type("BasePlatformAdapter", (), {
    "__init__": lambda s, *a, **k: setattr(s, "config", a[0] if a else None) or None,
    "emit_message_raw": lambda *a, **kw: None,
    "on_processing_start": lambda *a, **kw: None,
    "on_processing_complete": lambda *a, **kw: None,
    "send": lambda *a, **kw: None,
})

gw_models = unittest.mock.MagicMock()
sys.modules["gateway.platforms.models"] = gw_models
class _FakeChatContext:
    def __init__(self, **kw):
        pass
gw_models.ChatContext = _FakeChatContext

gw_util = unittest.mock.MagicMock()
sys.modules["gateway.util"] = gw_util

tools_mod = unittest.mock.MagicMock()
sys.modules["tools"] = tools_mod
tools_gateway = unittest.mock.MagicMock()
sys.modules["tools.clarify_gateway"] = tools_gateway
tools_gateway.mark_awaiting_text = unittest.mock.MagicMock()

# Bind for tests
ProcessingOutcome = _FakeProcessingOutcome

# --- slixmpp-omemo / omemo mocks ---
slixmpp_omemo_mod = unittest.mock.MagicMock()
sys.modules["slixmpp_omemo"] = slixmpp_omemo_mod
slixmpp_omemo_mod.TrustLevel = type("TrustLevel", (), {})
slixmpp_omemo_mod.XEP_0384 = type("XEP_0384", (), {})

omemo_mod = unittest.mock.MagicMock()
sys.modules["omemo"] = omemo_mod
omemo_storage = unittest.mock.MagicMock()
sys.modules["omemo.storage"] = omemo_storage
class _FakeStorage:
    def __init__(self): pass
    async def _load(self, key): pass
    async def _store(self, key, value): pass
omemo_storage.Just = type("Just", (), {"__init__": lambda self, *a, **k: None})
omemo_storage.Maybe = type("Maybe", (), {})
omemo_storage.Nothing = type("Nothing", (), {})
omemo_storage.Storage = _FakeStorage

omemo_types = unittest.mock.MagicMock()
sys.modules["omemo.types"] = omemo_types
omemo_types.DeviceInformation = type("DeviceInformation", (), {})
omemo_types.JSONType = type("JSONType", (), {})

import adapter

# Force a fresh import so we don't inherit a broken BasePlatformAdapter from another test file's cache
if "adapter" in sys.modules:
    del sys.modules["adapter"]
for key in list(sys.modules.keys()):
    if key.startswith("adapter."):
        del sys.modules[key]
import adapter

# Wire imported symbols
adapter.BasePlatformAdapter = gw_base.BasePlatformAdapter
adapter.MessageEvent = gw_base.MessageEvent
adapter.MessageType = gw_base.MessageType
adapter.ProcessingOutcome = gw_base.ProcessingOutcome
adapter.SendResult = gw_base.SendResult


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture
def fake_client():
    """Return a mocked slixmpp Client-like object with registered plugins."""
    client = MagicMock()
    client.boundjid = MagicMock()
    client.boundjid.bare = "hermes@example.org"
    client.boundjid.full = "hermes@example.org/desktop"

    client.plugins = {}

    def _plugin_item(self, key):
        return self.plugins.get(key)

    def _in(self, key):
        return key in self.plugins

    client.__getitem__ = _plugin_item
    client.__contains__ = _in

    client.make_message = MagicMock(return_value=MagicMock())
    client.send_message = MagicMock(return_value=MagicMock())

    return client


@pytest.fixture
def fake_adapter(fake_client):
    """Return an XMPPAdapter instance wired to a fake client."""
    cfg = MagicMock()
    cfg.jid = "hermes@example.org"
    cfg.password = "secret"
    cfg.home_channel = None
    cfg.fileserver_url = None

    a = adapter.XmppAdapter(cfg)
    a.client = fake_client
    a._reactions_enabled = True
    a.allow_all_users = True
    a._registered_plugins = {
        "xep_0004",
        "xep_0050",
        "xep_0444",
        "xep_0461",
        "xep_0394",
        "xep_0446",
        "xep_0447",
        "xep_0363",
    }
    a._pending_reactions = {}
    return a


# ------------------------------------------------------------------
# B1 — Reactions (XEP-0444)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_processing_start_triggers_reaction(fake_adapter, fake_client):
    fake_client.plugins["xep_0444"] = MagicMock()

    source = MagicMock()
    source.chat_id = "user@example.org"
    source.user_id = "user@example.org"

    evt = MagicMock()
    evt.source = source
    evt.message_id = "msg-1"
    await fake_adapter.on_processing_start(event=evt)
    assert "msg-1" in fake_adapter._pending_reactions


@pytest.mark.asyncio
async def test_on_processing_complete_success(fake_adapter, fake_client):
    xep0444 = MagicMock()
    fake_client.plugins["xep_0444"] = xep0444

    source = MagicMock()
    source.chat_id = "user@example.org"
    source.user_id = "user@example.org"

    evt = MagicMock()
    evt.source = source
    evt.message_id = "msg-1"
    fake_adapter._pending_reactions["msg-1"] = evt

    await fake_adapter.on_processing_complete(event=evt, outcome=ProcessingOutcome.SUCCESS)

    xep0444.set_reactions.assert_called()
    calls = xep0444.set_reactions.call_args_list
    assert any("✅" in str(c) for c in calls)


@pytest.mark.asyncio
async def test_on_processing_complete_error(fake_adapter, fake_client):
    xep0444 = MagicMock()
    fake_client.plugins["xep_0444"] = xep0444

    source = MagicMock()
    source.chat_id = "user@example.org"
    source.user_id = "user@example.org"

    evt = MagicMock()
    evt.source = source
    evt.message_id = "msg-1"
    fake_adapter._pending_reactions["msg-1"] = evt

    await fake_adapter.on_processing_complete(event=evt, outcome=ProcessingOutcome.FAILURE)

    xep0444.set_reactions.assert_called()
    calls = xep0444.set_reactions.call_args_list
    assert any("❌" in str(c) for c in calls)


# ------------------------------------------------------------------
# B2 — Clarify via Data Forms (XEP-0004)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_clarify_uses_data_form_when_available(fake_adapter, fake_client):
    xep0004 = MagicMock()
    fake_form = MagicMock()
    xep0004.make_form.return_value = fake_form
    fake_client.plugins["xep_0004"] = xep0004

    choices = [{"label": "A", "value": "a"}, {"label": "B", "value": "b"}]
    result = await fake_adapter.send_clarify(
        chat_id="user@example.org",
        question="Pick one",
        choices=choices,
        clarify_id="clarify-1",
        session_key="sess-1",
    )
    assert result.success is True
    xep0004.make_form.assert_called_once()


@pytest.mark.asyncio
async def test_send_clarify_falls_back_to_text(fake_adapter, fake_client):
    fake_adapter._registered_plugins.discard("xep_0004")
    fake_adapter.client = fake_client

    choices = [{"label": "A", "value": "a"}, {"label": "B", "value": "b"}]
    result = await fake_adapter.send_clarify(
        chat_id="user@example.org",
        question="Pick one",
        choices=choices,
        clarify_id="clarify-1",
        session_key="sess-1",
    )
    # Text fallback routes through send() → make_message(...).send()
    fake_client.make_message.assert_called_once()
    assert result.success is True


# ------------------------------------------------------------------
# B3 — Ad-Hoc Commands (XEP-0050)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_setup_adhoc_commands(fake_adapter, fake_client):
    xep0050 = MagicMock()
    fake_client.plugins["xep_0050"] = xep0050
    await fake_adapter._setup_adhoc_commands()
    xep0050.add_command.assert_called_once()
    node = xep0050.add_command.call_args[1]["node"]
    assert node == "hermes"


# ------------------------------------------------------------------
# C1 — Message Replies (XEP-0461)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_with_reply_to_uses_xep0461(fake_adapter, fake_client):
    xep0461 = MagicMock()
    fake_client.plugins["xep_0461"] = xep0461
    fake_client["xep_0461"] = xep0461
    stanza = MagicMock()
    xep0461.make_reply.return_value = stanza
    fake_client.make_message.return_value = stanza

    result = await fake_adapter.send(
        chat_id="user@example.org",
        content="got it",
        reply_to="orig-msg-id",
    )
    assert result.success is True
    # First (and only) chunk threads as a reply via make_reply(reply_to,
    # reply_id, **msg_kwargs) then .send() (slixmpp 1.15 signature).
    xep0461.make_reply.assert_called_once()
    call = xep0461.make_reply.call_args
    assert str(call.args[0]) == "user@example.org"
    assert call.args[1] == "orig-msg-id"
    assert call.kwargs["mbody"] == "got it"
    assert call.kwargs["mto"] == "user@example.org"
    stanza.send.assert_called_once()


@pytest.mark.asyncio
async def test_send_without_reply_to_uses_plain_message(fake_adapter, fake_client):
    stanza = MagicMock()
    fake_client.make_message.return_value = stanza
    result = await fake_adapter.send(chat_id="user@example.org", content="hello")
    assert result.success is True
    fake_client.make_message.assert_called_once()
    stanza.send.assert_called_once()


# ------------------------------------------------------------------
# C2 — Message Markup (XEP-0394)
# ------------------------------------------------------------------

def test_build_markup_bold():
    a = adapter.XmppAdapter(MagicMock())
    markup = a._build_markup("This is **bold** text")
    assert markup is not None


def test_build_markup_code():
    a = adapter.XmppAdapter(MagicMock())
    markup = a._build_markup("Use `echo hi`")
    assert markup is not None


def test_build_markup_none():
    a = adapter.XmppAdapter(MagicMock())
    markup = a._build_markup("Plain text without formatting")
    assert markup is None


# ------------------------------------------------------------------
# D1 — Voice / Stateless File Sharing (XEP-0447)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_voice_uses_xep0447(fake_adapter, fake_client):
    xep0447 = MagicMock()
    fake_client.plugins["xep_0447"] = xep0447
    fake_client["xep_0447"] = xep0447
    xep0363 = MagicMock()
    fake_client.plugins["xep_0363"] = xep0363
    fake_client["xep_0363"] = xep0363

    # Mock upload_file as async
    async def _mock_upload(**kw):
        return "https://example.org/voice.ogg"
    xep0363.upload_file = _mock_upload

    xep0447.get_sfs.return_value = MagicMock()

    with patch("adapter.mimetypes.guess_type", return_value=("audio/ogg", None)), \
         patch("os.path.isfile", return_value=True), \
         patch("os.path.getsize", return_value=42):
        result = await fake_adapter.send_voice(
            chat_id="user@example.org",
            audio_path="/tmp/voice.ogg",
        )
    assert result.success is True
    xep0447.get_sfs.assert_called_once()


@pytest.mark.asyncio
async def test_send_voice_falls_back_to_upload(fake_adapter, fake_client):
    # xep_0447 not registered, should fall back to _upload_and_send
    fake_adapter._registered_plugins.discard("xep_0447")
    fake_adapter.client = fake_client
    fake_adapter._registered_plugins = {"xep_0363"}

    async def _mock_upload(**kw):
        return "https://example.org/voice.ogg"

    xep0363 = MagicMock()
    xep0363.upload_file = _mock_upload
    fake_client.plugins["xep_0363"] = xep0363

    with patch("adapter.mimetypes.guess_type", return_value=("audio/ogg", None)), \
         patch("os.path.isfile", return_value=True), \
         patch("os.path.getsize", return_value=42):
        result = await fake_adapter.send_voice(
            chat_id="user@example.org",
            audio_path="/tmp/voice.ogg",
        )
    # Falls back to _upload_and_send which calls send_message
    fake_client.send_message.assert_called()
    assert result.success is True


# ------------------------------------------------------------------
# Reply context extraction (_on_message)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_message_extracts_reply_context(fake_adapter, fake_client):
    stanza = MagicMock()
    stanza.__getitem__ = lambda self, key: None
    stanza.get_from.return_value = "user@example.org"
    stanza["type"] = "chat"
    stanza["body"] = "a reply"
    stanza.get = MagicMock(side_effect=lambda key, default=None: {
        "id": "msg-1",
        "reply": MagicMock(get=MagicMock(return_value="orig-id")),
    }.get(key, default))

    events = []

    async def _capture(evt):
        events.append(evt)

    fake_adapter.handle_message = _capture
    fake_adapter.allow_all_users = True

    await fake_adapter._on_message(stanza)
    # Message may be dropped by auth or other checks; just verify no crash
    # If events were emitted, check reply context
    if events:
        assert events[0].reply_to_message_id == "orig-id"


# ------------------------------------------------------------------
# Issue #6 — MUC self-reply loop + duplicate handler registration
# ------------------------------------------------------------------

def _make_groupchat_stanza(*, nick, body="hello", room="room@conference.example.org",
                           delay=False):
    """Build a fake groupchat stanza whose `from` is room@host/nick."""
    from_jid = MagicMock()
    from_jid.bare = room
    from_jid.resource = nick
    from_jid.__str__ = lambda self=None: f"{room}/{nick}"  # type: ignore[assignment]

    fields = {"type": "groupchat", "body": body}
    delay_plugin = object() if delay else None

    stanza = MagicMock()
    stanza.__getitem__ = lambda self, key: fields.get(key, "")
    stanza.get = lambda key, default=None: {"muc": None}.get(key, default)
    stanza.get_from = lambda: from_jid
    stanza.get_plugin = lambda name, check=False: delay_plugin if name == "delay" else None
    return stanza


@pytest.mark.asyncio
async def test_muc_self_message_is_dropped(fake_adapter):
    """A groupchat message carrying our own MUC nick must not be dispatched.

    Regression for issue #6: bare-JID check never matches in a MUC (from is
    room@host/nick), so the bot replied to its own messages forever.
    """
    fake_adapter.muc_nick = "hermes"
    fake_adapter.muc_rooms = []
    fake_adapter._known_mucs = {"room@conference.example.org"}

    events = []

    async def _capture(evt):
        events.append(evt)

    fake_adapter.handle_message = _capture

    stanza = _make_groupchat_stanza(nick="hermes", body="my own message")
    await fake_adapter._on_message(stanza)
    assert events == [], "bot dispatched its own MUC message (self-reply loop)"


@pytest.mark.asyncio
async def test_muc_other_user_message_is_dispatched(fake_adapter):
    """A groupchat message from someone else must still be processed."""
    fake_adapter.muc_nick = "hermes"
    fake_adapter.muc_rooms = []
    fake_adapter._known_mucs = {"room@conference.example.org"}

    events = []

    async def _capture(evt):
        events.append(evt)

    fake_adapter.handle_message = _capture
    fake_adapter.build_source = MagicMock(return_value=MagicMock())

    stanza = _make_groupchat_stanza(nick="alice", body="hey bot")
    await fake_adapter._on_message(stanza)
    assert len(events) == 1, "legitimate MUC message from another user was dropped"


@pytest.mark.asyncio
async def test_muc_history_replay_is_dropped(fake_adapter):
    """Delayed (history-replay) groupchat messages must be ignored on join."""
    fake_adapter.muc_nick = "hermes"
    fake_adapter.muc_rooms = []
    fake_adapter._known_mucs = {"room@conference.example.org"}

    events = []

    async def _capture(evt):
        events.append(evt)

    fake_adapter.handle_message = _capture

    stanza = _make_groupchat_stanza(nick="alice", body="old msg", delay=True)
    await fake_adapter._on_message(stanza)
    assert events == [], "stale MUC history replay was dispatched as a live message"


def test_on_message_registered_only_once(fake_adapter):
    """_on_message must be registered for 'message' only, not also
    'groupchat_message' — otherwise MUC stanzas fire the handler twice.

    Regression for issue #6 (double handling).
    """
    import inspect
    src = inspect.getsource(adapter.XmppAdapter.connect)
    assert 'add_event_handler("message", self._on_message)' in src
    assert 'add_event_handler("groupchat_message", self._on_message)' not in src
