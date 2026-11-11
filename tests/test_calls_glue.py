"""Audio-call glue: config, controller wiring, reply capture, Hermes-backed speech.

The call engine itself (Jingle, media, conversation) is tested in the calls library; this file
covers only what the adapter adds. Requires aiortc/av/numpy (requirements-calls.txt).

Run: python -m pytest tests/test_calls_glue.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import threading
import types
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("aiortc")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from conftest import evict_adapter_modules, restore_sys_modules, snapshot_sys_modules

_MODULES_SNAPSHOT = snapshot_sys_modules()
evict_adapter_modules()

for name in ("gateway", "gateway.config", "gateway.platforms", "gateway.platforms.base",
             "gateway.platforms.models", "gateway.util", "tools", "tools.clarify_gateway"):
    sys.modules[name] = MagicMock()
sys.modules["gateway.config"].Platform = type("Platform", (), {"__init__": lambda self, *a, **k: None})
sys.modules["gateway.config"].PlatformConfig = type("PlatformConfig", (), {"__init__": lambda self, *a, **k: None})
gw_base = sys.modules["gateway.platforms.base"]
gw_base.MessageEvent = MagicMock
gw_base.MessageType = type("MessageType", (), {"TEXT": "text", "VOICE": "voice"})
gw_base.ProcessingOutcome = type("ProcessingOutcome", (), {"SUCCESS": 0, "FAILURE": 1, "CANCELLED": 2})
gw_base.SendResult = type("SendResult", (), {"__init__": lambda s, **kw: s.__dict__.update(kw) or None})
gw_base.BasePlatformAdapter = type("BasePlatformAdapter", (), {
    "__init__": lambda self, *a, **k: None,
    "build_source": lambda s, **kw: types.SimpleNamespace(**kw),
    "_release_platform_lock": lambda self: None,
    "_mark_disconnected": lambda self: None,
})

import adapter  # noqa: E402

restore_sys_modules(_MODULES_SNAPSHOT)


def make_adapter(**extra):
    cfg = MagicMock()
    cfg.extra = {"jid": "hermes@example.org", "password": "test-only",
                 "allowed_users": "user@example.org", **extra}
    a = adapter.XmppAdapter(cfg)
    a._self_bare = "hermes@example.org"
    return a


# ---------------------------------------------------------------- settings

def run_loop_in_thread():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    return loop, thread


def stop_loop(loop, thread):
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()  # an unclosed loop is a ResourceWarning, which CI turns into a failure


def test_env_enables_calls(monkeypatch):
    monkeypatch.setenv("XMPP_CALLS_ENABLED", "true")
    assert adapter._calls_settings({})["enabled"] is True


# ---------------------------------------------------------------- wiring

def test_disabled_builds_no_controller():
    a = make_adapter()
    assert a._build_calls(MagicMock()) is None
    assert a._calls_host is None


def test_enabled_builds_controller_and_installs_handlers():
    a = make_adapter(calls={"enabled": True})
    client = MagicMock()
    controller = a._build_calls(client)
    assert controller is not None
    assert a._calls_host is not None
    assert client.register_handler.call_count == 6  # JMI x4, received carbon, Jingle
    client.register_plugin.assert_called_with("xep_0115")


def test_missing_dependencies_keep_chat_alive(monkeypatch):
    monkeypatch.setitem(sys.modules, "calls.hermes_host", None)  # import raises ImportError
    a = make_adapter(calls={"enabled": True})
    assert a._build_calls(MagicMock()) is None
    assert a._calls_host is None


def test_authorization_follows_the_allow_list():
    a = make_adapter(calls={"enabled": True})
    a._build_calls(MagicMock())
    assert a._calls_host.authorized("user@example.org")
    assert not a._calls_host.authorized("stranger@example.org")


# ---------------------------------------------------------------- reply capture

def test_send_is_captured_only_for_the_pending_call_turn():
    a = make_adapter(calls={"enabled": True})
    a._build_calls(MagicMock())
    a.client = MagicMock()

    async def scenario():
        host = a._calls_host
        turn = host._pending["user@example.org"] = type("T", (), {})()
        turn.message_id, turn.parts = "call-1-1", []
        taken = await a.send("user@example.org/phone", "Fine, thanks.", reply_to="call-1-1")
        other = host.capture("user@example.org", "unrelated", "some-other-id")
        return taken, other, turn.parts

    taken, other, parts = asyncio.run(scenario())
    assert taken.success is True
    assert other is False
    assert parts == ["Fine, thanks."]


def test_processing_complete_resolves_the_turn(monkeypatch):
    a = make_adapter(calls={"enabled": True})
    a._build_calls(MagicMock())
    for key, module in (("gateway", MagicMock()), ("gateway.platforms", MagicMock()),
                        ("gateway.platforms.base", gw_base)):
        monkeypatch.setitem(sys.modules, key, module)  # hermes_host imports these lazily

    async def scenario():
        host = a._calls_host
        seen = {}

        async def fake_handle(event):
            seen["event"] = event
            host.capture("user@example.org", "First part.", event.message_id)
            host.capture("user@example.org", "Second part.", event.message_id)
            await a.on_processing_complete(event, 0)

        a.handle_message = fake_handle
        a._reactions_allowed = lambda event: False
        reply = await host.ask("user@example.org", "sid", "what time is it")
        return reply, seen["event"]

    reply, event = asyncio.run(scenario())
    assert reply == "First part.\nSecond part."
    assert event.message_id.startswith("call-sid-")
    assert "phone call" in event.channel_prompt


def test_disconnect_hangs_up_calls():
    a = make_adapter(calls={"enabled": True})
    closed = []

    async def close():
        closed.append(True)

    a._calls = types.SimpleNamespace(close=close)
    a.client = None
    asyncio.run(a.disconnect())
    assert closed == [True]
    assert a._calls is None


# ---------------------------------------------------------------- Hermes-backed speech

def test_speech_uses_hermes_stt_and_tts(tmp_path, monkeypatch):
    import numpy as np
    from calls.hermes_host import HermesSpeech

    wav_path = tmp_path / "speech.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(np.zeros(24000, np.int16).tobytes())

    tools = types.ModuleType("tools")
    stt = types.ModuleType("tools.transcription_tools")
    stt.transcribe_audio = lambda path, model=None, source=None: {"success": True, "transcript": " hello  there "}
    tts = types.ModuleType("tools.tts_tool")
    tts.text_to_speech_tool = lambda text: json.dumps({"success": True, "file_path": str(wav_path)})
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", stt)
    monkeypatch.setitem(sys.modules, "tools.tts_tool", tts)

    speech = HermesSpeech()
    assert asyncio.run(speech.transcribe(np.zeros(16000, np.int16))) == "hello there"
    audio = asyncio.run(speech.synthesize("Hi."))
    assert 47000 <= len(audio) <= 49000  # 1 s at 48 kHz
    assert not wav_path.exists()  # cleaned up after decoding


# ---------------------------------------------------------------- xmpp_call tool

def test_tool_reports_disabled_when_no_adapter_is_live(monkeypatch):
    monkeypatch.setattr(adapter, "_CALL_ADAPTERS", {})
    assert adapter._calls_available() is False
    out = json.loads(adapter._xmpp_call_tool({"opening": "hi"}))
    assert out["success"] is False and "not enabled" in out["status"]


def test_tool_places_the_call_on_the_adapter_loop(monkeypatch):
    import threading

    a = make_adapter(calls={"enabled": True})
    loop, thread = run_loop_in_thread()
    seen = []

    async def place_call(peer, opening, **options):
        seen.append((peer, opening, threading.current_thread() is not threading.main_thread(), options))
        return "connected: the call is up"

    a._calls = types.SimpleNamespace(place_call=place_call)
    a._calls_loop = loop
    monkeypatch.setattr(adapter, "_CALL_ADAPTERS", {adapter._home_key(): a})
    try:
        out = json.loads(adapter._xmpp_call_tool(
            {"opening": "Your backup finished.", "uninterruptible": True, "hang_up_after": "false"}))
    finally:
        stop_loop(loop, thread)
    assert out == {"success": True, "status": "connected: the call is up"}
    assert seen == [("user@example.org", "Your backup finished.", True,
                     {"uninterruptible": True, "hang_up_after": False, "topic": "", "context": "", "attempts": 1, "pause": None})]
    assert adapter._calls_available() is True


def test_tool_needs_a_target_when_several_users_are_allowed(monkeypatch):
    a = make_adapter(calls={"enabled": True}, allowed_users="a@example.org,b@example.org")
    a._calls, a._calls_loop = object(), object()
    monkeypatch.setattr(adapter, "_CALL_ADAPTERS", {adapter._home_key(): a})
    out = json.loads(adapter._xmpp_call_tool({"opening": "hi"}))
    assert out["success"] is False and "say who to call" in out["status"]


def test_register_adds_the_tool_and_survives_old_hermes():
    ctx = MagicMock()
    adapter.register(ctx)
    names = [c.kwargs["name"] for c in ctx.register_tool.call_args_list]
    assert names == ["xmpp_call", "xmpp_message"]
    assert ctx.register_platform.called

    old = MagicMock()
    old.register_tool.side_effect = AttributeError
    adapter.register(old)
    assert old.register_platform.called


def test_session_mode_decides_the_thread_a_call_runs_in():
    a = make_adapter(calls={"enabled": True})
    a._build_calls(MagicMock())
    source = a._calls_host._source("user@example.org", "sid42")
    assert (source.chat_id, source.chat_type, source.thread_id) == ("user@example.org", "dm", "call-sid42")
    a = make_adapter(calls={"enabled": True, "session": "per_peer", "session_title": "Calls {name}"})
    controller = a._build_calls(MagicMock())
    for call_id in ("sid1", "sid2"):  # every call of the person shares one session
        assert a._calls_host._source("user@example.org", call_id).thread_id == "calls"
    assert controller.config.session == "per_peer"
    assert controller.config.session_title == "Calls {name}"


def test_per_peer_rotation_puts_the_period_in_the_thread_and_pins_it_per_call(monkeypatch):
    import datetime as real

    clock = [real.datetime(2026, 11, 12, 23, 59)]

    clock_module = types.SimpleNamespace(now=lambda: clock[0])

    def thread(rotate, call_id, session="per_peer"):
        a = make_adapter(calls={"enabled": True, "session": session, "session_rotate": rotate})
        a._build_calls(MagicMock())
        monkeypatch.setattr(sys.modules[type(a._calls_host).__module__], "datetime", clock_module)
        return a._calls_host, a._calls_host._source("user@example.org", call_id).thread_id

    for rotate, want in [
        ("never", "calls"),
        ("daily", "calls-2026-11-12"),
        ("weekly", "calls-2026-W46"),
        ("monthly", "calls-2026-11"),
    ]:
        assert thread(rotate, "a")[1] == want
    assert thread("daily", "a", session="per_call")[1] == "call-a"  # rotation is per_peer only
    host, first = thread("daily", "late")
    clock[0] = real.datetime(2026, 11, 13, 0, 1)  # the call runs past midnight
    assert host._source("user@example.org", "late").thread_id == first  # same session
    assert host._source("user@example.org", "next").thread_id == "calls-2026-11-13"


def test_a_call_session_is_titled_once_and_with_a_title_hermes_has_not_taken(monkeypatch):
    class Db:
        def __init__(self, titles):
            self.titles = titles  # session id -> title

        async def get_session_title(self, session_id):
            return self.titles.get(session_id)

        async def get_next_title_in_lineage(self, base):
            taken = [t for t in self.titles.values() if t and t.split(" #")[0] == base]
            return f"{base} #{len(taken) + 1}" if taken else base

    class Store:
        async def get_or_create_session(self, source):
            return types.SimpleNamespace(session_id="s-now")

    sessions = types.SimpleNamespace(
        SessionDB=types.SimpleNamespace(sanitize_title=lambda title: title.strip())
    )
    monkeypatch.setitem(sys.modules, "hermes_state", sessions)
    a = make_adapter(calls={"enabled": True})
    host = a._build_calls(MagicMock()) and a._calls_host

    def free(title, db=None, store=None):
        a.gateway_runner = types.SimpleNamespace(_session_db=db, async_session_store=store)
        return asyncio.run(host._free_title(object(), title))

    assert free("Calls with sam") == "Calls with sam"  # no session database: sent as it is
    assert free(" Calls with sam ", Db({}), Store()) == "Calls with sam"
    assert free("Calls with sam", Db({"s-old": "Calls with sam"}), Store()) == "Calls with sam #2"
    assert free("Calls with sam", Db({"s-1": "Calls with sam", "s-2": "Calls with sam #2"}), Store()) == "Calls with sam #3"
    assert free("Calls with sam", Db({"s-now": "Calls with sam"}), Store()) is None  # keep its title
    long = "x" * 100  # Hermes allows 100 characters: " #2" must not push it over
    numbered = free(long, Db({"s-old": long}), Store())
    assert len(numbered) <= 100 and numbered != long

    class Broken(Db):
        async def get_session_title(self, session_id):
            raise RuntimeError("database is locked")

    assert free("Calls with sam", Broken({}), Store()) == "Calls with sam"  # never blocks the call


def test_steer_sends_a_steer_command_for_the_pending_turn(monkeypatch):
    a = make_adapter(calls={"enabled": True})
    a._build_calls(MagicMock())
    monkeypatch.setitem(sys.modules, "gateway", MagicMock())
    monkeypatch.setitem(sys.modules, "gateway.platforms", MagicMock())
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", gw_base)
    sent = []

    async def handle(event):
        sent.append(event)

    a.handle_message = handle
    host = a._calls_host

    async def scenario():
        refused = await host.steer("user@example.org", "sid", "also check email")
        host._pending["user@example.org"] = types.SimpleNamespace(message_id="call-sid-1", parts=[])
        return refused, await host.steer("user@example.org", "sid", "also check email")

    refused, accepted = asyncio.run(scenario())
    assert refused is False and accepted is True
    assert sent[0].text == "/steer also check email"
    assert sent[0].message_id.startswith("call-steer-")
    assert sent[0].source.thread_id == "call-sid"
    assert host.capture("user@example.org", "ack", sent[0].message_id) is True  # ack stays out of chat


def test_each_profile_in_one_gateway_finds_its_own_adapter(monkeypatch, tmp_path):
    """multiplex_profiles: two profiles with calls in one process must not share an adapter."""
    homes = {"default": tmp_path / "default", "smartphone": tmp_path / "smartphone"}
    current = {"home": homes["default"]}
    monkeypatch.setattr(adapter, "_hermes_home", lambda: str(current["home"]))
    monkeypatch.setattr(adapter, "_CALL_ADAPTERS", {})
    adapters = {}
    for name, home in homes.items():
        current["home"] = home
        a = make_adapter(calls={"enabled": True})
        a._calls = a._build_calls(MagicMock())  # as connect() does
        a._register_for_tools()
        adapters[name] = a
    for name, home in homes.items():
        current["home"] = home
        assert adapter._live_adapter() is adapters[name]
    current["home"] = homes["smartphone"]
    asyncio.run(adapters["smartphone"].disconnect())
    assert adapter._live_adapter() is None
    current["home"] = homes["default"]
    assert adapter._live_adapter() is adapters["default"]


def test_partner_agent_may_chat_but_never_phone(monkeypatch):
    """The phone profile allows the default agent to message it; calls stay human-only."""
    a = make_adapter(
        allowed_users="user@example.org,agent@example.org",
        calls={"enabled": True, "allowed_users": ["User@Example.org"]},
    )
    a._build_calls(MagicMock())
    host = a._calls_host
    assert host.authorized("user@example.org")
    assert not host.authorized("agent@example.org")  # allowed to chat, not to call
    assert a._is_authorized(chat_type="dm", chat_id="agent@example.org", user_jid="agent@example.org")
    a._calls, a._calls_loop = object(), object()
    monkeypatch.setattr(adapter, "_CALL_ADAPTERS", {adapter._home_key(): a})
    # with two allowed users, the tool still knows whom to call
    import threading

    loop, thread = run_loop_in_thread()
    seen = []

    async def place_call(peer, opening, **options):
        seen.append(peer)
        return "connected: up"

    a._calls, a._calls_loop = types.SimpleNamespace(place_call=place_call), loop
    try:
        out = json.loads(adapter._xmpp_call_tool({"opening": "hi"}))
    finally:
        stop_loop(loop, thread)
    assert out["success"] is True and seen == ["user@example.org"]


def test_plaintext_peers_are_never_omemo_encrypted():
    a = make_adapter(plaintext_peers=["Agent@Example.org"])
    a._registered_plugins = {"xep_0384"}
    on = adapter.SLIXMPP_OMEMO_AVAILABLE
    assert a._use_omemo("user@example.org", "chat") is on
    assert a._use_omemo("agent@example.org/res", "chat") is False
    assert a._use_omemo("room@conference.example.org", "groupchat") is False


def test_callable_people_exclude_partner_agents_by_default():
    a = make_adapter(
        allowed_users="user@example.org,wife@example.org,agent@example.org",
        plaintext_peers=["agent@example.org"],
        calls={"enabled": True},
    )
    a._build_calls(MagicMock())
    host = a._calls_host
    assert host.callable_jids() == {"user@example.org", "wife@example.org"}
    assert host.authorized("wife@example.org")
    assert not host.authorized("agent@example.org")


def test_ambiguous_target_lists_the_choices(monkeypatch):
    a = make_adapter(allowed_users="a@example.org,b@example.org", calls={"enabled": True})
    a._calls = a._build_calls(MagicMock())
    a._calls_loop = object()
    monkeypatch.setattr(adapter, "_CALL_ADAPTERS", {adapter._home_key(): a})
    out = json.loads(adapter._xmpp_call_tool({"opening": "hi"}))
    assert out["success"] is False
    assert "a@example.org, b@example.org" in out["status"]


def test_omemo_device_lists_are_downloaded_once_per_ten_minutes():
    a = make_adapter()
    calls = []

    async def refresh(jids, force_download=False):
        calls.append((sorted(str(j) for j in jids), force_download))

    a.client = MagicMock()
    a.client.__getitem__.return_value.refresh_device_lists = refresh

    async def run():
        await a._refresh_omemo_devices("User@Example.org/phone")
        await a._refresh_omemo_devices("user@example.org")  # throttled
        await a._refresh_omemo_devices("other@example.org")

    asyncio.run(run())
    assert calls == [(["user@example.org"], True), (["other@example.org"], True)]


def test_omemo_download_times_are_pruned_so_the_dict_does_not_grow_forever(monkeypatch):
    monkeypatch.setattr(adapter.time, "monotonic", lambda: 10_000.0)  # not the host's uptime
    a = make_adapter()
    a.client = MagicMock()
    a.client.__getitem__.return_value.refresh_device_lists = AsyncMock()
    a._omemo_refreshed = {f"old{i}@example.org": 0.0 for i in range(100)}
    asyncio.run(a._refresh_omemo_devices("new@example.org"))
    assert set(a._omemo_refreshed) == {"new@example.org"}


def test_partner_agent_messages_are_rate_limited(monkeypatch):
    """Two agents answering each other must not loop forever."""
    a = make_adapter(agent_peers=["Agent@Example.org"], agent_peer_limit=2)
    assert a._agent_peers == {"agent@example.org"}
    assert all(a._agent_peer_may_speak("agent@example.org") for _ in range(2))
    assert not a._agent_peer_may_speak("agent@example.org")  # the next one is dropped
    assert a._agent_peer_may_speak("other@example.org")  # others are untouched
    real = adapter.time.monotonic
    monkeypatch.setattr(adapter.time, "monotonic", lambda: real() + 601)
    assert a._agent_peer_may_speak("agent@example.org")  # window passed


def test_agent_peers_are_not_callable_and_the_tool_says_who_is(monkeypatch):
    a = make_adapter(
        allowed_users="user@example.org,agent@example.org",
        agent_peers=["agent@example.org"],
        calls={"enabled": True},
    )
    a._calls = a._build_calls(MagicMock())
    a._calls_loop = object()
    monkeypatch.setattr(adapter, "_CALL_ADAPTERS", {adapter._home_key(): a})
    assert a._calls_host.callable_jids() == {"user@example.org"}
    out = json.loads(adapter._xmpp_call_tool({"opening": "hi", "to": "agent@example.org"}))
    assert out["success"] is False and "user@example.org" in out["status"]


def test_acknowledgements_of_title_and_steer_stay_out_of_the_chat():
    a = make_adapter(calls={"enabled": True})
    a._build_calls(MagicMock())
    host = a._calls_host
    host._silent.add("call-title-abc")
    assert host.capture("user@example.org", "Session title set", "call-title-abc") is True
    assert host.capture("user@example.org", "Session title set", "other-id") is False  # normal chat


def test_status_lines_from_a_partner_agent_are_not_answered():
    assert adapter._is_gateway_notice("⚙️ tool_search")
    assert adapter._is_gateway_notice("⚡ Interrupting current task (iteration 2).")
    assert adapter._is_gateway_notice("⚠️ The model returned only a silence marker for a message")
    for line in (  # lines the gateway itself writes while a turn is still running or retrying
        "⏸️ This conversation is paused: a previous turn did not finish when Hermes restarted",
        "💡 First-time tip — I queued your message instead of interrupting.",
        "⚠️ I seem to be stuck (no activity for 5 min). Send /stop to cancel the current run.",
        "⚠️ No response from provider for 60s (model: x). Retrying",
        "⚠️ Rate limited — switching to fallback provider...",
        "🗜️ Compacted earlier messages", "📦 Preflight compression: ~90,000 tokens",
        "♻️ Gateway online/restarted", "🔁 Response dominated by repeated text",
    ):
        assert adapter._is_gateway_notice(line), line
    assert not adapter._is_gateway_notice("CALL REQUEST for x")
    for answer in ("⚠️ Disk is almost full on host-1", "✅ Deployed build 42", "❌ The deploy failed: exit 1"):
        assert not adapter._is_gateway_notice(answer), answer  # real results and warnings pass
    assert "[SILENT]" in adapter._AGENT_PROMPT


def test_status_lines_are_not_sent_to_a_partner_agent():
    a = make_adapter(agent_peers=["agent@example.org"])
    a.client = MagicMock()
    result = asyncio.run(a.send("agent@example.org", "⚠️ The model returned only a silence marker"))
    assert result.success
    a.client.make_message.assert_not_called()


def test_one_shot_sender_never_uses_omemo(monkeypatch):
    """It shares the account (and the OMEMO store file) with the live gateway adapter."""
    seen = {}

    async def fake_connect(self, *a, **k):
        seen["omemo"] = self._omemo_enabled
        self.fatal_error_message = "stop here"
        return False

    monkeypatch.setattr(adapter.XmppAdapter, "connect", fake_connect)
    monkeypatch.setattr(adapter.XmppAdapter, "disconnect", lambda self: asyncio.sleep(0))
    monkeypatch.setattr(adapter, "check_xmpp_requirements", lambda: True)
    cfg = MagicMock()
    cfg.extra = {"jid": "hermes@example.org", "password": "x", "omemo": {"enabled": True}}
    asyncio.run(adapter.send_xmpp_message(cfg, "user@example.org", "hi"))
    assert seen["omemo"] is False


def test_call_request_topic_and_context_reach_the_tool_without_the_model(monkeypatch):
    a = make_adapter(allowed_users="user@example.org", agent_peers=["agent@example.org"],
                     calls={"enabled": True})
    a._note_call_request(
        "CALL REQUEST for x: topic=daily-note. Phone now.\nREF: slug=a commit=b\n"
        "EDIT PLAYBOOK: edit it.\nBRIEFING:\nGood morning."
    )
    _, topic, context = a._call_request
    assert topic == "daily-note"
    assert context == "REF: slug=a commit=b\nEDIT PLAYBOOK: edit it."
    a._note_call_request("hello there")  # not a request: nothing changes
    assert a._call_request[1] == "daily-note"

    loop, thread = run_loop_in_thread()
    seen = []

    async def place_call(peer, opening, **options):
        seen.append(options)
        return "connected: up"

    a._calls, a._calls_loop = types.SimpleNamespace(place_call=place_call), loop
    monkeypatch.setattr(adapter, "_CALL_ADAPTERS", {adapter._home_key(): a})
    try:
        json.loads(adapter._xmpp_call_tool({"opening": "hi"}))
    finally:
        stop_loop(loop, thread)
    assert seen[0]["topic"] == "daily-note"
    assert "EDIT PLAYBOOK" in seen[0]["context"]


def test_xmpp_message_tool_sends_only_to_known_addresses(monkeypatch):
    a = make_adapter(allowed_users="user@example.org", agent_peers=["agent@example.org"],
                     calls={"enabled": True})
    loop, thread = run_loop_in_thread()
    sent = []

    async def send(chat_id, content, reply_to=None, **kw):
        sent.append((chat_id, content, reply_to))
        return types.SimpleNamespace(success=True, error=None)

    a.send, a._calls_loop = send, loop
    monkeypatch.setattr(adapter, "_CALL_ADAPTERS", {adapter._home_key(): a})
    try:
        ok = json.loads(adapter._xmpp_message_tool({"to": "Agent@Example.org", "text": "do it"}))
        bad = json.loads(adapter._xmpp_message_tool({"to": "evil@example.org", "text": "hi"}))
    finally:
        stop_loop(loop, thread)
    assert ok == {"success": True, "status": "sent"}
    assert sent == [("agent@example.org", "do it", "xmpp-message-tool")]
    assert bad["success"] is False and "agent@example.org" in bad["status"]


def test_a_silence_warning_is_never_spoken_the_agent_is_asked_again():
    a = make_adapter(calls={"enabled": True})
    a._build_calls(MagicMock())
    host = a._calls_host
    answers = iter(["⚠️ The model returned only a silence marker for a message.", "Goodbye. [[hangup]]"])
    seen = []

    async def once(peer, call_id, text):
        seen.append(text)
        return next(answers)

    host._ask_once = once
    assert asyncio.run(host.ask("user@example.org", "sid", "goodbye")) == "Goodbye. [[hangup]]"
    assert seen[1].endswith("goodbye") and seen[1].startswith("[Your last reply was empty")

    host._ask_once = lambda p, c, t: asyncio.sleep(0, "⚠️ The model returned only a silence marker.")
    assert asyncio.run(host.ask("user@example.org", "sid", "hi")) == ""  # still silent: say nothing


def test_a_stt_command_that_hears_nothing_is_an_empty_transcript(monkeypatch):
    mod = types.ModuleType("tools.transcription_tools")
    mod.transcribe_audio = lambda *a, **k: {
        "success": False,
        "error": "Command STT provider wrote no output file at /x/transcript.txt and produced no stdout",
    }
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", mod)
    a = make_adapter(calls={"enabled": True})
    a._build_calls(MagicMock())
    import numpy as np

    assert asyncio.run(a._calls_host.speech.transcribe(np.zeros(1600, np.int16))) == ""
    mod.transcribe_audio = lambda *a, **k: {"success": False, "error": "service down"}
    with pytest.raises(RuntimeError):
        asyncio.run(a._calls_host.speech.transcribe(np.zeros(1600, np.int16)))


# ------------------------------------------------------------ OMEMO key safety

omemo = pytest.mark.skipif(not adapter.SLIXMPP_OMEMO_AVAILABLE, reason="slixmpp-omemo not installed")


@omemo
def test_omemo_store_is_atomic_and_private(tmp_path):
    path = tmp_path / "omemo.json"
    store = adapter._StorageImpl(path)
    asyncio.run(store._store("/ikp/key", "secret"))
    assert json.loads(path.read_text()) == {"/ikp/key": "secret"}
    assert (path.stat().st_mode & 0o777) == 0o600
    assert not list(tmp_path.glob("*.tmp"))  # the temp file was renamed over the store


@omemo
def test_omemo_store_write_syncs_the_file_and_its_directory(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(os.fstat(fd).st_mode), real(fd)))
    store = adapter._StorageImpl(tmp_path / "omemo.json")
    asyncio.run(store._store("/a", "one"))
    assert [stat.S_ISDIR(mode) for mode in synced] == [False, True]  # the file, then the rename


@omemo
def test_omemo_store_that_cannot_be_read_is_never_overwritten(tmp_path):
    path = tmp_path / "omemo.json"
    path.write_text('{"/ikp/key": "secret"')  # cut off mid-write
    with pytest.raises(adapter.OmemoStoreError):
        adapter._StorageImpl(path)
    assert path.read_text() == '{"/ikp/key": "secret"'  # untouched: nothing was reset


@omemo
def test_omemo_store_that_is_empty_is_never_replaced(tmp_path):
    path = tmp_path / "omemo.json"
    path.write_text("")  # what a crash between truncating and writing leaves
    with pytest.raises(adapter.OmemoStoreError, match="empty"):
        adapter._StorageImpl(path)
    assert path.read_text() == ""  # no new identity was written over it


@omemo
def test_omemo_store_is_refused_to_a_second_process(tmp_path):
    import fcntl
    import os

    path = tmp_path / "omemo.json"
    other = os.open(f"{os.path.realpath(path)}.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(other, fcntl.LOCK_EX)  # another process holds the store
    try:
        with pytest.raises(adapter.OmemoStoreError):
            adapter._StorageImpl(path)
    finally:
        os.close(other)
    adapter._StorageImpl(path)  # free again
    adapter._StorageImpl(path)  # the same process may reopen it (reconnects)


@omemo
def test_world_readable_key_file_is_locked_down(tmp_path):
    path = tmp_path / "omemo.json"
    path.write_text("{}")
    path.chmod(0o644)
    adapter._StorageImpl(path)
    assert (path.stat().st_mode & 0o777) == 0o600


def test_xmpp_message_tool_is_rate_limited(monkeypatch):
    a = make_adapter(allowed_users="user@example.org", agent_peers=["agent@example.org"], message_tool_limit=3, calls={"enabled": True})
    loop, thread = run_loop_in_thread()

    async def send(chat_id, content, reply_to=None, **kw):
        return types.SimpleNamespace(success=True, error=None)

    a.send, a._calls_loop = send, loop
    monkeypatch.setattr(adapter, "_CALL_ADAPTERS", {adapter._home_key(): a})
    try:
        results = [json.loads(adapter._xmpp_message_tool({"to": "user@example.org", "text": str(i)})) for i in range(a._message_tool_limit + 1)]
    finally:
        stop_loop(loop, thread)
    assert all(r["success"] for r in results[:-1])
    assert results[-1]["success"] is False and "too many" in results[-1]["status"]


def test_xmpp_call_tool_refuses_a_long_opening_and_limits_calls(monkeypatch):
    a = make_adapter(allowed_users="user@example.org", calls={"enabled": True, "max_calls_per_hour": 2, "opening_max_chars": 100})
    loop, thread = run_loop_in_thread()

    class Controller:
        async def place_call(self, *args, **kwargs):
            return "connected: ok"

    a._calls, a._calls_loop = Controller(), loop
    monkeypatch.setattr(adapter, "_CALL_ADAPTERS", {adapter._home_key(): a})
    try:
        long = json.loads(adapter._xmpp_call_tool({"to": "user@example.org", "opening": "x" * 101}))
        assert not long["success"] and "too long" in long["status"] and "at most 100" in long["status"]
        results = [
            json.loads(adapter._xmpp_call_tool({"to": "user@example.org", "opening": "hi"}))
            for _ in range(3)
        ]
    finally:
        stop_loop(loop, thread)
    assert all(r["success"] for r in results[:-1])
    assert not results[-1]["success"] and "too many calls" in results[-1]["status"]

@omemo
def test_two_storages_for_one_file_in_a_process_do_not_overwrite_each_other(tmp_path):
    path = tmp_path / "omemo.json"
    first, second = adapter._StorageImpl(path), adapter._StorageImpl(path)  # e.g. a reconnect
    asyncio.run(first._store("/a", "one"))
    asyncio.run(second._store("/b", "two"))
    assert json.loads(path.read_text()) == {"/a": "one", "/b": "two"}
