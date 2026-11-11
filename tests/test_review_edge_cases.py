"""Regressions for the adapter review; all storage is synthetic test data."""

from __future__ import annotations

import asyncio
import importlib.util
import importlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import restore_fake_modules, snapshot_sys_modules

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_adapter(reload_module=None):
    snapshot = snapshot_sys_modules()
    sys.modules.pop("adapter", None)
    for name in ("gateway", "gateway.config", "gateway.platforms", "gateway.platforms.base"):
        sys.modules[name] = types.ModuleType(name)
    config = sys.modules["gateway.config"]
    config.Platform = lambda name: name
    config.PlatformConfig = types.SimpleNamespace
    base = sys.modules["gateway.platforms.base"]
    base.BasePlatformAdapter = type(
        "BasePlatformAdapter",
        (),
        {
            "__init__": lambda self, *args: None,
            "build_source": lambda self, **kw: types.SimpleNamespace(**kw),
        },
    )
    base.MessageEvent = types.SimpleNamespace
    base.MessageType = types.SimpleNamespace(TEXT="text", VOICE="voice")
    base.ProcessingOutcome = types.SimpleNamespace(SUCCESS=0, FAILURE=1, CANCELLED=2)
    base.SendResult = types.SimpleNamespace
    try:
        if reload_module is not None:
            sys.modules["adapter"] = reload_module
            return importlib.reload(reload_module)
        return importlib.import_module("adapter")
    finally:
        restore_fake_modules(snapshot)


adapter = load_adapter()


needs_numpy = pytest.mark.skipif(
    importlib.util.find_spec("numpy") is None, reason="the call dependencies are not installed"
)


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setattr(adapter, "_scoped_getenv", lambda key, default=None: default)


def make_adapter(**extra):
    return adapter.XmppAdapter(
        types.SimpleNamespace(
            extra={
                "jid": "bot@example.org",
                "password": "fixture-only",
                "allowed_users": "user@example.org",
                **extra,
            }
        )
    )


GARBAGE = [float("inf"), -float("inf"), float("nan"), "inf", "NaN", None, "bad"]
NUMERIC_CALL_KEYS = [
    "max_seconds", "filler_after", "ring_timeout", "silence_ms", "vad_level", "hangup_grace",
    "idle_timeout", "busy_wait", "ring_attempts", "ring_pause", "max_calls_per_hour", "opening_max_chars",
]


def test_call_settings_defaults_overrides_and_garbage():
    """One table per behaviour; every failing case is listed, not just the first."""
    defaults = adapter._calls_settings({})
    failures = []

    def check(label, got, want):
        if got != want:
            failures.append(f"{label}: got {got!r}, want {want!r}")

    def setting(key, value):
        return adapter._calls_settings({"calls": {key: value}})[key]

    check("off by default", (defaults["enabled"], defaults["filler"], defaults["barge_in"]), (False, None, "queue"))
    check("defaults", (defaults["greeting"], defaults["ack_tone"], defaults["warn_tone"], defaults["max_seconds"],
                       defaults["max_calls_per_hour"], defaults["opening_max_chars"]),
          (("Hi, I'm here. What can I do for you?",), True, True, 600, 6, 1500))
    check("adapter uses them", make_adapter()._calls_cfg, defaults)
    peer_defaults = make_adapter()
    check("peer limits", (peer_defaults._agent_peer_limit, peer_defaults._message_tool_limit), (4, 10))
    cfg = adapter._calls_settings({"calls": {"enabled": True, "max_seconds": "120", "filler_after": 2}})
    check("overrides", (cfg["enabled"], cfg["max_seconds"], cfg["filler_after"]), (True, 120, 2.0))
    cfg = adapter._calls_settings({"calls": {"vad_level": 9, "silence_ms": 1200}})
    check("clamp", (cfg["vad_level"], cfg["silence_ms"]), (3, 1200))
    check("barge_in", [setting("barge_in", v) for v in ("STOP", "whatever")], ["stop", "queue"])
    check("bad words", (setting("max_seconds", "soon"), setting("filler_after", None)), (600, 3.0))
    for value, want in [("Hi.", ("Hi.",)), ([" A. ", "B.", ""], ("A.", "B.")), ("", ()), ([], ()), (None, ()), (False, ())]:
        check(f"greeting={value!r}", setting("greeting", value), want)
    check("greeting list is capped", len(setting("greeting", [f"g{i}" for i in range(50)])), 20)
    for value, want in [("Hold on.", ("Hold on.",)), ([" A. ", "B.", ""], ("A.", "B.")), ("", ()), ([], ()), (False, ()),
                        ([None, {"a": 1}, "ok"], ("ok",)), (None, None)]:
        check(f"filler={value!r}", setting("filler", value), want)
    check("filler list is capped", len(setting("filler", [f"p{i}" for i in range(50)])), 20)
    home = adapter._hermes_home()
    for value, want in [(True, True), ("on", True), (None, True), (False, False), ("", False), ("off", False),
                        ("/usr/share/beep.wav", "/usr/share/beep.wav"), ("beep.wav", f"{home}/beep.wav"),
                        ("~/beep.wav", os.path.expanduser("~/beep.wav"))]:
        check(f"ack_tone={value!r}", setting("ack_tone", value), want)
        check(f"warn_tone={value!r}", setting("warn_tone", value), want)
    for value, want in [(False, None), ("off", None), ("false", None), (True, 600), (None, 600), (45, 45)]:
        check(f"max_seconds={value!r}", setting("max_seconds", value), want)
    for key in NUMERIC_CALL_KEYS:  # garbage falls back even with calls off
        for bad in GARBAGE:
            got = make_adapter(calls={"enabled": False, key: bad})._calls_cfg
            check(f"{key}={bad!r}", (got[key], got["enabled"]), (defaults[key], False))
    for key, value, want in [
        ("max_seconds", -5, 600), ("max_seconds", 0, 600), ("ring_timeout", -1, 60), ("ring_timeout", 0, 60),
        ("silence_ms", -1, 800), ("silence_ms", 0, 800), ("filler_after", -1, 3.0), ("ring_attempts", 10**9, 5),
        ("max_calls_per_hour", 0, 1), ("max_calls_per_hour", 10**9, 100), ("max_calls_per_hour", 12, 12),
        ("opening_max_chars", 5, 50), ("opening_max_chars", 10**9, 10000), ("opening_max_chars", 3000, 3000),
    ]:
        check(f"{key}={value}", setting(key, value), want)
    limited = make_adapter(agent_peer_limit=8, message_tool_limit=20)
    check("peer limits set", (limited._agent_peer_limit, limited._message_tool_limit), (8, 20))
    for bad in GARBAGE + [0, -3, 10**9]:
        got = make_adapter(agent_peer_limit=bad, message_tool_limit=bad)
        if (got._agent_peer_limit, got._message_tool_limit) not in ((4, 10), (1000, 1000), (1, 1)):
            failures.append(f"peer limits {bad!r}: {(got._agent_peer_limit, got._message_tool_limit)}")
    extra = adapter._apply_yaml_config({}, {"jid": "a@b", "password": "x", "calls": {"enabled": True}, "agent_peer_limit": 9})
    check("yaml", (extra["calls"], extra["agent_peer_limit"]), ({"enabled": True}, 9))
    assert not failures, "\n".join(failures)


def test_numeric_inputs_never_crash_or_pass_nonfinite_values():
    failures = []
    for key, attr, default in [
        ("max_message_length", "MAX_MESSAGE_LENGTH", 10000),
        ("connect_timeout_secs", "_CONNECT_TIMEOUT_SECS", 180.0),
        ("port", "port", 5222),
    ]:
        failures += [f"{key}={bad!r}" for bad in GARBAGE if bad is not None and getattr(make_adapter(**{key: bad}), attr) != default]
    failures += [
        f"bounded({bad!r})" for bad in GARBAGE
        if adapter._bounded_int(bad, 1, 5) is not None or adapter._bounded_float(bad, 0.0, 3600.0) is not None
    ]
    if adapter._bounded_float(10**1000, 0.0, 3600.0) is not None:  # float() overflows
        failures.append("bounded_float(10**1000)")
    assert not failures, failures


async def deliver(
    instance, body, *, encrypted=True, wire_body="This message is OMEMO encrypted.", fail=False
):
    class Stanza(dict):
        def get_from(self):
            return adapter.JID("agent@example.org/phone")

        def get_plugin(self, *args, **kwargs):
            return None

    stanza = Stanza(type="chat", body=wire_body if encrypted else body, id="fixture-message")
    if encrypted:
        plugin = types.SimpleNamespace(
            is_encrypted=lambda _: True,
            decrypt_message=AsyncMock(
                side_effect=ValueError("synthetic decrypt failure") if fail else None,
                return_value=({"body": body}, types.SimpleNamespace(device_id=1)),
            ),
        )
        instance.client = MagicMock()
        instance.client.__getitem__.return_value = plugin
        instance._registered_plugins = {"xep_0384"}
    instance._voice_transcription_enabled = False
    await instance._on_message(stanza)


@pytest.mark.parametrize("encrypted", [False, True])
def test_partner_notices_do_not_dispatch_or_exhaust_real_message_quota(encrypted):
    instance = make_adapter(allowed_users="agent@example.org", agent_peers=["agent@example.org"])
    instance.handle_message = AsyncMock()

    async def scenario():
        for _ in range(adapter._AGENT_PEER_LIMIT + 1):
            await deliver(instance, "⚙️ tool_search", encrypted=encrypted)
        await deliver(instance, "Please do the work", encrypted=encrypted)

    asyncio.run(scenario())
    assert [call.args[0].text for call in instance.handle_message.await_args_list] == [
        "Please do the work"
    ]
    assert len(instance._agent_peer_times["agent@example.org"]) == 1


@pytest.mark.parametrize("encrypted", [False, True])
def test_partner_call_request_uses_final_body_and_only_after_rate_guard(encrypted):
    instance = make_adapter(allowed_users="agent@example.org", agent_peers=["agent@example.org"])
    instance.handle_message = AsyncMock()
    request = "CALL REQUEST topic=deploy\nREF: fixture\nBRIEFING:\nPlease call."
    asyncio.run(deliver(instance, request, encrypted=encrypted))
    assert instance._call_request[1:] == ("deploy", "REF: fixture")
    assert instance.handle_message.await_args.args[0].text == request
    instance._agent_peer_times["agent@example.org"] *= adapter._AGENT_PEER_LIMIT
    asyncio.run(deliver(instance, "CALL REQUEST topic=blocked\nother context", encrypted=encrypted))
    assert instance._call_request[1:] == ("deploy", "REF: fixture")
    assert instance.handle_message.await_count == 1


def test_encrypted_partner_plaintext_notice_is_not_used_as_final_body():
    instance = make_adapter(allowed_users="agent@example.org", agent_peers=["agent@example.org"])
    instance.handle_message = AsyncMock()
    asyncio.run(deliver(instance, "Please work", wire_body="⚙️ tool_search"))
    assert instance.handle_message.await_args.args[0].text == "Please work"


def test_failed_partner_decryption_does_not_charge_quota_or_record_request():
    instance = make_adapter(allowed_users="agent@example.org", agent_peers=["agent@example.org"])
    instance.handle_message = AsyncMock()
    asyncio.run(deliver(instance, "", wire_body="CALL REQUEST topic=untrusted", fail=True))
    assert instance._agent_peer_times == {}
    assert instance._call_request[1:] == ("", "")
    instance.handle_message.assert_not_awaited()


@pytest.fixture
def live_tools(monkeypatch):
    import threading

    from calls.hermes_host import HermesCallHost

    instance = make_adapter(calls={"enabled": True})
    instance._calls_host = HermesCallHost(instance)
    seen = []

    async def place_call(peer, opening, **options):
        if not instance._calls_host.authorized(peer):
            return "refused: not authorized"
        seen.append((peer, options))
        return "connected: fixture"

    instance._calls = types.SimpleNamespace(place_call=place_call)
    instance.send = AsyncMock(return_value=types.SimpleNamespace(success=True))
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    instance._calls_loop = loop
    monkeypatch.setattr(adapter, "_CALL_ADAPTERS", {adapter._home_key(): instance})
    try:
        yield instance, seen
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        assert not thread.is_alive()
        loop.close()


@needs_numpy
@pytest.mark.parametrize(
    "target", ["USER@EXAMPLE.ORG", " user@example.org ", " User@Example.org/Phone "]
)
def test_call_tool_normalizes_target_before_host_authorization(live_tools, target):
    _, seen = live_tools
    result = json.loads(adapter._xmpp_call_tool({"to": target, "opening": "Hello"}))
    assert result["success"] is True, result
    assert seen[0][0] == "user@example.org"


@needs_numpy
@pytest.mark.parametrize(
    "target", ["USER@EXAMPLE.ORG", " user@example.org ", " User@Example.org/Phone "]
)
def test_message_tool_uses_the_same_normalized_recipient(live_tools, target):
    instance, _ = live_tools
    result = json.loads(adapter._xmpp_message_tool({"to": target, "text": "Hello"}))
    assert result["success"] is True, result
    assert instance.send.await_args.kwargs["chat_id"] == "user@example.org"


@needs_numpy
def test_call_tool_nonfinite_overrides_use_safe_defaults(live_tools):
    _, seen = live_tools
    result = json.loads(
        adapter._xmpp_call_tool(
            {
                "opening": "Hello",
                "attempts": float("inf"),
                "pause_seconds": float("nan"),
            }
        )
    )
    assert result["success"] is True
    assert seen[0][1]["attempts"] == 1
    assert seen[0][1]["pause"] is None


@needs_numpy
def test_tools_still_refuse_normalized_unknown_targets(live_tools):
    instance, seen = live_tools
    target = " Stranger@Example.org/Phone "
    assert not json.loads(adapter._xmpp_call_tool({"to": target, "opening": "Hello"}))["success"]
    assert not json.loads(adapter._xmpp_message_tool({"to": target, "text": "Hello"}))["success"]
    assert seen == []
    instance.send.assert_not_awaited()


def test_message_tool_works_with_calls_off_and_no_call_libraries(monkeypatch):
    """The hand-off worker only sends text: it must not need calls.enabled or aiortc."""
    import threading

    monkeypatch.setattr(adapter, "_CALL_ADAPTERS", {})
    instance = make_adapter()  # calls are off
    instance.send = AsyncMock(return_value=types.SimpleNamespace(success=True))
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()

    async def connect_side() -> None:
        instance._register_for_tools()

    try:
        asyncio.run_coroutine_threadsafe(connect_side(), loop).result(5)
        assert adapter._xmpp_connected() and not adapter._calls_available()
        sent = json.loads(adapter._xmpp_message_tool({"to": "user@example.org", "text": "hi"}))
        refused = json.loads(adapter._xmpp_call_tool({"to": "user@example.org", "opening": "hi"}))
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()
    assert sent == {"success": True, "status": "sent"}
    assert refused["success"] is False
    assert "calls are not enabled" in refused["status"]


def test_slow_call_package_build_does_not_hold_up_chat_start(caplog):
    """The gateway gives connect() 30 s: a slow first build carries on in its thread."""
    import threading

    instance = make_adapter(calls={"enabled": True})
    release = threading.Event()
    instance._provide_call_packages = release.wait

    async def scenario():
        try:
            await instance._await_call_packages(timeout=0.05)
        finally:
            release.set()  # inside the loop: asyncio.run joins the thread on exit

    asyncio.run(scenario())
    assert "still being prepared" in caplog.text
