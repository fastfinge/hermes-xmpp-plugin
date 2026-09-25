"""Multiplexed-gateway credential isolation (the 2026-09-25 alice/hannah/charlotte bug).

Under ``multiplex_profiles`` one process serves every profile; ``os.environ``
belongs to the LAUNCH profile. A secondary adapter that reads raw
``os.getenv("XMPP_JID")`` binds the launch profile's JID (all three XMPP
accounts connected as alice@; each owner's DM then hit the wrong profile's
allowlist and got a pairing prompt naming the wrong profile). These tests pin
the scoped resolution: per-profile secret scope first, fail-closed under
multiplexing, legacy single-profile behaviour preserved otherwise.

Follows the conftest discipline: snapshot sys.modules, install fakes
(``agent.secret_scope`` — not installed in a standalone plugin checkout),
import adapter, restore.
"""
from __future__ import annotations

import os
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from conftest import snapshot_sys_modules, restore_sys_modules  # noqa: E402

_snap = snapshot_sys_modules()

# --- fake gateway (so adapter.py imports in a standalone plugin checkout) -----
import unittest.mock

_gateway_mod = unittest.mock.MagicMock()
sys.modules["gateway"] = _gateway_mod

_gw_config = unittest.mock.MagicMock()
sys.modules["gateway.config"] = _gw_config
_gw_config.Platform = type("Platform", (), {"__init__": lambda self, *a, **k: None, "value": "xmpp"})
_gw_config.PlatformConfig = type("PlatformConfig", (), {"__init__": lambda self, *a, **k: None})

_gw_platforms = unittest.mock.MagicMock()
sys.modules["gateway.platforms"] = _gw_platforms

_gw_base = unittest.mock.MagicMock()
sys.modules["gateway.platforms.base"] = _gw_base


class _FakeBase:
    """Faithful-enough base: stores config, nothing else — subclass assigns."""

    def __init__(self, *a, **k):
        self.config = a[0] if a else None


_gw_base.BasePlatformAdapter = _FakeBase


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


_gw_base.MessageEvent = _FakeMessageEvent
_gw_base.MessageType = type("MessageType", (), {"TEXT": "text", "IMAGE": "image", "COMMAND": "command"})
_gw_base.ProcessingOutcome = type("ProcessingOutcome", (), {"SUCCESS": 0, "FAILURE": 1, "CANCELLED": 2})

# --- fake agent.secret_scope -------------------------------------------------
_agent = ModuleType("agent")
_secret_scope = ModuleType("agent.secret_scope")

_STATE = {"scope": None, "multiplex": False}


class UnscopedSecretError(RuntimeError):
    pass


def set_scope(mapping):
    _STATE["scope"] = mapping


def set_multiplex(active):
    _STATE["multiplex"] = bool(active)


def get_secret(name, default=None):
    scope = _STATE["scope"]
    if scope is not None:
        return scope.get(name, default)
    if _STATE["multiplex"]:
        raise UnscopedSecretError(name)
    if default is not None:
        return default
    return os.environ.get(name)


def is_multiplex_active():
    return _STATE["multiplex"]


_agent.secret_scope = _secret_scope
_secret_scope.get_secret = get_secret
_secret_scope.is_multiplex_active = is_multiplex_active
_secret_scope.UnscopedSecretError = UnscopedSecretError
sys.modules["agent"] = _agent
sys.modules["agent.secret_scope"] = _secret_scope

import adapter  # noqa: E402

# NOTE: no restore_sys_modules() here, unlike import-time-only suites — adapter
# resolves agent.secret_scope LAZILY at call time, so the fakes must stay in
# sys.modules for the whole file. Cross-file safety: the autouse reset below
# returns the fake to its neutral state, where get_secret() is behaviourally
# identical to the module being absent (falls back to os.environ), so a later
# test file's adapter copy is unaffected.


def _reset(monkeypatch, *, multiplex=False, scope=None):
    monkeypatch.delenv("XMPP_JID", raising=False)
    monkeypatch.delenv("XMPP_PASSWORD", raising=False)
    monkeypatch.delenv("XMPP_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    adapter._DOTENV_PEEK_CACHE.clear()
    _STATE["scope"] = scope
    _STATE["multiplex"] = multiplex


_ENV_NAMES = ("XMPP_JID", "XMPP_PASSWORD", "XMPP_ALLOWED_USERS", "HERMES_HOME")


@pytest.fixture(autouse=True)
def _own_fakes_installed(monkeypatch):
    """Re-install THIS file's fakes before every test.

    Adapter resolves agent.secret_scope lazily at CALL time, so whichever fake
    a later-collected file left in sys.modules would otherwise win. Re-binding
    here makes each file self-sufficient regardless of collection order.
    """
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    adapter._DOTENV_PEEK_CACHE.clear()
    sys.modules["agent"] = _agent
    sys.modules["agent.secret_scope"] = _secret_scope
    _STATE["scope"] = None
    _STATE["multiplex"] = False
    yield
    _STATE["scope"] = None
    _STATE["multiplex"] = False


def _make_cfg(**extra):
    cfg = MagicMock()
    base = {"jid": "hermes@example.org", "password": "secret", "omemo_enabled": "false"}
    base.update(extra)
    cfg.extra = base
    return cfg


# --- _scoped_getenv ----------------------------------------------------------

def test_scope_wins_over_process_env(monkeypatch):
    _reset(monkeypatch, scope={"XMPP_JID": "secondary@profile.example"})
    monkeypatch.setenv("XMPP_JID", "launch@env.example")
    assert adapter._scoped_getenv("XMPP_JID") == "secondary@profile.example"


def test_unscoped_multiplex_fails_closed(monkeypatch):
    """THE bug: multiplexed, no scope → never borrow the launch profile's env."""
    _reset(monkeypatch, multiplex=True)
    monkeypatch.setenv("XMPP_JID", "alice@launch.example")
    assert adapter._scoped_getenv("XMPP_JID") == ""


def test_multiplex_missing_key_in_scope_fails_closed(monkeypatch):
    _reset(monkeypatch, multiplex=True, scope={"XMPP_JID": "hannah@example"})
    monkeypatch.setenv("XMPP_ALLOWED_USERS", "launch-owner@example")
    assert adapter._scoped_getenv("XMPP_ALLOWED_USERS") == ""


def test_single_profile_keeps_legacy_env_resolution(monkeypatch):
    _reset(monkeypatch, multiplex=False, scope=None)
    monkeypatch.setenv("XMPP_JID", "plain@example.org")
    assert adapter._scoped_getenv("XMPP_JID") == "plain@example.org"


def test_tuning_vars_never_peek_the_env_file(monkeypatch, tmp_path):
    """Only the credential pair may fall back to the .env peek — a developer
    machine's XMPP_CONNECT_TIMEOUT_SECS in ~/.hermes/.env must not override the
    class default for a bare config."""
    (tmp_path / ".env").write_text("XMPP_CONNECT_TIMEOUT_SECS=1\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _reset(monkeypatch, multiplex=False, scope=None)
    assert adapter._scoped_getenv("XMPP_CONNECT_TIMEOUT_SECS") == ""


def test_credentials_peek_the_env_file(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("XMPP_JID=peeked@example.org\n")
    _reset(monkeypatch, multiplex=False, scope=None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert adapter._scoped_getenv("XMPP_JID", peek=True) == "peeked@example.org"


# --- adapter wiring ----------------------------------------------------------

def test_adapter_reads_jid_from_scope(monkeypatch):
    _reset(
        monkeypatch,
        scope={"XMPP_JID": "hannah@profile.example", "XMPP_PASSWORD": "pw",
               "XMPP_ALLOWED_USERS": "cathie@example"},
    )
    a = adapter.XmppAdapter(_make_cfg(jid=None, password=None))
    assert a.jid == "hannah@profile.example"
    assert a._password == "pw"
    assert a.allowed_users == {"cathie@example"}


def test_adapter_fails_closed_under_multiplex(monkeypatch):
    """A multiplexed secondary with no scope and no explicit jid must NOT bind
    the launch profile's JID — it gets an empty jid (validate_config False),
    which surfaces as a clear credential error instead of the wrong bot."""
    monkeypatch.setenv("XMPP_JID", "alice@launch.example")
    monkeypatch.setenv("XMPP_PASSWORD", "shared-pw")
    _reset(monkeypatch, multiplex=True)
    a = adapter.XmppAdapter(_make_cfg(jid=None, password=None))
    assert a.jid == ""
    assert a._password == ""
    assert adapter.validate_config(a.config) is False


# --- yaml export guard -------------------------------------------------------

def test_apply_yaml_config_does_not_export_under_multiplex(monkeypatch):
    """Under multiplexing the process env is shared: one profile's yaml block
    must never write another tenant's XMPP_* env."""
    _reset(monkeypatch, multiplex=True)
    for name in ("XMPP_JID", "XMPP_PASSWORD", "XMPP_ALLOWED_USERS"):
        monkeypatch.delenv(name, raising=False)
    extra = adapter._apply_yaml_config({}, {
        "jid": "hannah@example", "password": "pw", "allowed_users": ["cathie@example"],
    })
    assert extra["jid"] == "hannah@example"
    assert "XMPP_JID" not in os.environ
    assert "XMPP_ALLOWED_USERS" not in os.environ


def test_apply_yaml_config_still_exports_single_profile(monkeypatch):
    _reset(monkeypatch, multiplex=False)
    for name in ("XMPP_JID", "XMPP_PASSWORD", "XMPP_ALLOWED_USERS"):
        monkeypatch.delenv(name, raising=False)
    extra = adapter._apply_yaml_config({}, {
        "jid": "bot@example", "password": "pw", "allowed_users": ["sam@example"],
    })
    try:
        assert extra["jid"] == "bot@example"
        assert os.environ["XMPP_JID"] == "bot@example"
        assert os.environ["XMPP_ALLOWED_USERS"] == "sam@example"
    finally:
        for name in ("XMPP_JID", "XMPP_PASSWORD", "XMPP_ALLOWED_USERS"):
            monkeypatch.delenv(name, raising=False)


# --- OMEMO home --------------------------------------------------------------

def test_omemo_default_storage_resolves_home(monkeypatch, tmp_path):
    _reset(monkeypatch, multiplex=False, scope=None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = adapter.XmppAdapter(_make_cfg(omemo_enabled="false"))
    assert a._omemo_storage_path == str(tmp_path / "xmpp_omemo.json")
