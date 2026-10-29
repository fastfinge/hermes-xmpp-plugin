"""Teardown/lifetime regression tests for issue #12.

XMPP teardown must stop the background tasks that slixmpp and slixmpp-omemo
start for a client and then never stop themselves:

- slixmpp's XMLStream.connect() spawns the ``run_filters`` out-filter pump
  (a ``while True`` loop over the send queue). disconnect() never cancels
  it — the only canceller is XMLStream.__del__, which the pump's own
  reference to the stream prevents from ever running.
- omemo's SessionManager.initialize() spawns
  ``__manage_signed_pre_key_rotation`` (a ``while True`` signed-pre-key
  rotation / bundle re-upload loop). Only SessionManager.shutdown() stops
  it, and nothing on the disconnect path called that.

In a long-running gateway both tasks survived every reconnect, pinning
their clients and managers (issue #12 measured 207 of each and 361,358
queued stanza entries across the retained clients).

These tests exercise REAL dependency lifecycle behavior where practical: a
real ClientXMPP running the genuine run_filters pump (the same
ensure_future call XMLStream.connect() makes, minus DNS/TCP), and a real
omemo SessionManager whose real ``__manage_signed_pre_key_rotation``
coroutine runs as a real asyncio task exactly as initialize() arranges it.
Mock-based client tests (test_regression's disconnect tests) keep passing
unchanged — but mocks alone would hide the internally created tasks this
issue is about.
"""
from __future__ import annotations

import asyncio
import gc
import sys
import time
import unittest.mock
import weakref
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# -----------------------------------------------------------------
# Fakes for the gateway/tools layer ONLY — slixmpp, slixmpp_omemo and
# omemo stay real: this file exists precisely to exercise their real
# task lifecycles. Snapshot/restore keeps these fakes from leaking to
# other test files and keeps this file from inheriting theirs.
# -----------------------------------------------------------------
from conftest import snapshot_sys_modules, restore_fake_modules

_MODULES_SNAPSHOT = snapshot_sys_modules()

for key in list(sys.modules.keys()):
    if key == "adapter" or key.startswith("adapter.") or key.startswith("gateway") \
            or key == "tools" or key.startswith("tools."):
        del sys.modules[key]

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


# A faithful-enough BasePlatformAdapter fake: real fatal-error/lock/notify
# bookkeeping so connect()/disconnect() run end-to-end (same contract as
# test_connect_resilience's fake).
release_calls = []


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
    pass


def _base_acquire_platform_lock(self, scope, identity, resource_desc):
    self._platform_lock_scope = scope
    self._platform_lock_identity = identity
    return True


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

import adapter  # noqa: E402

# Fakes are baked into this module's ``adapter``; restore the real world for
# the faked roots only. The real slixmpp/slixmpp-omemo/omemo modules that
# ``import adapter`` pulled in must survive in sys.modules: a global restore
# would delete them and split the real plugin registry across two module
# copies (see conftest.restore_fake_modules).
restore_fake_modules(_MODULES_SNAPSHOT)

import slixmpp  # noqa: E402  (real — imported after restore like everything else)

# Guard: the registry adapter's register_plugin() writes must be the same one
# the client's PluginManager reads, or registration fails with a spurious
# PluginNotFound even when OMEMO is fine.
import slixmpp.plugins.base as _slixmpp_plugins_base  # noqa: E402

assert adapter.register_plugin.__globals__.get("PLUGIN_REGISTRY") is \
    getattr(_slixmpp_plugins_base, "PLUGIN_REGISTRY", None), \
    "slixmpp.plugins.base was imported twice — plugin registry is fragmented"


# -----------------------------------------------------------------
# Helpers: offline real client + real OMEMO manager
# -----------------------------------------------------------------

def _make_adapter(tmp_path) -> adapter.XmppAdapter:
    cfg = MagicMock()
    cfg.jid = "hermes@example.org"
    cfg.password = "secret"
    cfg.home_channel = None
    cfg.fileserver_url = None
    cfg.extra = {
        "jid": "hermes@example.org",
        "password": "secret",
        "omemo_enabled": "true",
        "omemo_storage_path": str(tmp_path / "omemo.json"),
    }
    return adapter.XmppAdapter(cfg)


class _OfflineClient(slixmpp.ClientXMPP):
    """Real ClientXMPP whose connect() runs the offline slice of
    XMLStream.connect(): it spawns the genuine ``run_filters`` out-filter
    pump exactly the way slixmpp does (the
    ``self._run_out_filters = asyncio.ensure_future(self.run_filters())``
    of connect()), then leaves the stream unconnected — no DNS, no sockets.
    """

    def connect(self, host=None, port=None, **kwargs):
        if self._run_out_filters is None or self._run_out_filters.done():
            self._run_out_filters = asyncio.ensure_future(self.run_filters())
        self.disconnect_reason = None
        self._connect_loop_wait = 0
        future = asyncio.get_event_loop().create_future()
        future.set_result(None)
        return future


def _attach_real_manager(client):
    """Put a real omemo SessionManager on the client's real xep_0384 plugin
    in the state initialize() leaves it in after session_bind: the real
    ``__manage_signed_pre_key_rotation`` coroutine running as a real asyncio
    task, cached where get_session_manager() caches it.
    """
    from omemo.types import AsyncFramework
    from slixmpp_omemo.xep_0384 import _make_session_manager

    plugin = client["xep_0384"]
    manager_cls = _make_session_manager(client, plugin)
    manager = manager_cls.__new__(manager_cls)
    # Same mangled attribute surface _create_oldmemo_only_session_manager()
    # sets on the legacy fallback, plus the async-framework + task state a
    # fully initialized manager has (that is what SessionManager.shutdown()
    # reads and cancels).
    manager._SessionManager__backends = []
    manager._SessionManager__async_framework = AsyncFramework.ASYNCIO
    manager._SessionManager__own_bare_jid = "hermes@example.org"
    manager._SessionManager__own_device_id = 1
    rotation_coro = manager._SessionManager__manage_signed_pre_key_rotation(
        3600.0, AsyncFramework.ASYNCIO
    )
    manager._SessionManager__signed_pre_key_management_task = asyncio.ensure_future(
        rotation_coro
    )
    plugin._XEP_0384__session_manager = manager
    return manager


def _retained_dependency_tasks():
    """Qualnames of live tasks whose coroutine is one of the
    dependency-owned loops issue #12 reported leaking."""
    leaked = []
    for task in asyncio.all_tasks():
        code = getattr(task.get_coro(), "cr_code", None)
        qualname = getattr(code, "co_qualname", "") or ""
        if "run_filters" in qualname or "manage_signed_pre_key_rotation" in qualname:
            leaked.append(qualname)
    return leaked


# -----------------------------------------------------------------
# 1. Real dependency lifecycle: disconnect() stops the leaked tasks
# -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_disconnect_stops_real_slixmpp_and_omemo_background_tasks(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "ClientXMPP", _OfflineClient)
    a = _make_adapter(tmp_path)
    assert await a.connect() is True

    client = a.client
    assert client is not None
    pump_task = client._run_out_filters
    assert pump_task is not None and not pump_task.done()

    manager = _attach_real_manager(client)
    rotation_task = manager._SessionManager__signed_pre_key_management_task
    assert not rotation_task.done()

    # The queue bookkeeping from the issue: a client with no session parks
    # stanzas in __queued_stanzas; they were retained with the old client.
    client._XMLStream__queued_stanzas.append(("dead stanza", True))
    client._XMLStream__queued_stanzas.append(("dead stanza 2", False))

    await a.disconnect()

    assert a.client is None
    assert pump_task.done()
    assert rotation_task.done()
    assert client._XMLStream__queued_stanzas == []
    assert _retained_dependency_tasks() == []


@pytest.mark.asyncio
async def test_repeated_connect_disconnect_cycles_return_to_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "ClientXMPP", _OfflineClient)
    a = _make_adapter(tmp_path)
    assert _retained_dependency_tasks() == []

    refs = []
    for _ in range(5):
        assert await a.connect() is True
        client = a.client
        manager = _attach_real_manager(client)
        refs.append(weakref.ref(client))
        refs.append(weakref.ref(manager))
        await a.disconnect()
        # Acceptance: after every teardown the previous client's and
        # manager's tasks are gone, not accumulating per cycle.
        assert _retained_dependency_tasks() == []

    del client, manager
    # One extra loop tick before judging liveness: the final iteration's
    # freshly cancelled tasks release their frames (and their captured
    # clients) only after the loop processes the last completion callback —
    # this is transient tail bookkeeping, not a leak, so pumping once here
    # is part of letting teardown "finish", not papering over anything.
    await asyncio.sleep(0)
    gc.collect()
    alive = [r for r in refs if r() is not None]
    assert alive == [], f"{len(alive)} old client(s)/manager(s) still retained"


# -----------------------------------------------------------------
# 2. Safety: no collateral damage, bounded waits, ordering
# -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_disconnect_does_not_cancel_unrelated_tasks(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "ClientXMPP", _OfflineClient)
    a = _make_adapter(tmp_path)
    bystander = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    try:
        await a.connect()
        _attach_real_manager(a.client)
        await a.disconnect()
        assert not bystander.done()
    finally:
        bystander.cancel()
        try:
            await bystander
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_send_queue_flush_runs_before_pump_teardown(tmp_path, monkeypatch):
    """Ordering guard for disconnect(): the out-filter pump drains the send
    queue during the flush (run_filters calls waiting_queue.task_done()),
    so it must still be running while client.disconnect() flushes. Tearing
    the pump down first would wedge the flush into its full timeout on
    every disconnect()."""
    monkeypatch.setattr(adapter, "ClientXMPP", _OfflineClient)
    a = _make_adapter(tmp_path)
    await a.connect()
    client = a.client
    pump_task = client._run_out_filters

    pump_alive_during_flush = []
    orig_disconnect = client.disconnect

    def _disconnect(*args, **kwargs):
        pump_alive_during_flush.append(not pump_task.done())
        return orig_disconnect(*args, **kwargs)

    monkeypatch.setattr(client, "disconnect", _disconnect)

    await a.disconnect()
    assert pump_alive_during_flush == [True]
    assert pump_task.done()


@pytest.mark.asyncio
async def test_disconnect_is_bounded_when_manager_shutdown_hangs(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "ClientXMPP", _OfflineClient)
    a = _make_adapter(tmp_path)
    a._TEARDOWN_TIMEOUT_SECS = 0.05
    await a.connect()
    client = a.client
    manager = _attach_real_manager(client)
    rotation_task = manager._SessionManager__signed_pre_key_management_task

    async def _wedged():
        await asyncio.sleep(3600)

    monkeypatch.setattr(manager, "shutdown", _wedged)

    start = time.monotonic()
    await asyncio.wait_for(a.disconnect(), timeout=2.0)
    elapsed = time.monotonic() - start
    assert elapsed < 1.0
    # Stop signals were issued before any wait, so the wedged shutdown()
    # did not keep the rotation task (or anything else) alive.
    assert rotation_task.done()
    assert a.client is None


# -----------------------------------------------------------------
# 3. Partial initialization and idempotency
# -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_disconnect_handles_partial_initialization_and_is_idempotent(tmp_path):
    a = _make_adapter(tmp_path)
    # Never connected: nothing to tear down — and calling it twice is fine.
    await a.disconnect()
    await a.disconnect()
    assert a.client is None

    # A client without slixmpp plugin-registry internals (the MagicMock
    # shape the pre-existing tests use) and OMEMO never initialized must
    # degrade to "nothing to stop" — never crash, never touch anything.
    a2 = _make_adapter(tmp_path)
    fake = MagicMock()
    fake.disconnect = lambda: None
    a2.client = fake
    await a2.disconnect()
    assert a2.client is None


# -----------------------------------------------------------------
# 4. Adapter-level contract (the issue's isolated reproduction shape)
# -----------------------------------------------------------------

class _FakeManager:
    def __init__(self, rotation_task):
        self._SessionManager__signed_pre_key_management_task = rotation_task
        self.shutdown_calls = 0

    async def shutdown(self):
        self.shutdown_calls += 1
        self._SessionManager__signed_pre_key_management_task.cancel()
        try:
            await self._SessionManager__signed_pre_key_management_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_manager_shutdown_is_invoked_and_awaited(tmp_path):
    """Mirror of the issue's isolated teardown reproduction: the OMEMO
    manager's shutdown() must be called and awaited, and the manager's
    rotation task must be gone, even though the adapter never held a
    direct reference to that task."""
    a = _make_adapter(tmp_path)
    a._TEARDOWN_TIMEOUT_SECS = 1.0

    rotation = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    slow_filter = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    manager = _FakeManager(rotation)

    client = MagicMock()
    client.disconnect = lambda: None
    plugin = MagicMock()
    plugin._XEP_0384__session_manager = manager
    plugin._XEP_0384__session_manager_task = None
    client.plugin._plugins = {"xep_0384": plugin}
    client._run_out_filters = None
    client._XMLStream__slow_tasks = [slow_filter]
    client._XMLStream__queued_stanzas = [("stale", True)]

    a.client = client
    await a.disconnect()

    assert manager.shutdown_calls == 1
    assert rotation.done()
    assert slow_filter.done()
    assert client._XMLStream__queued_stanzas == []
    assert a.client is None
