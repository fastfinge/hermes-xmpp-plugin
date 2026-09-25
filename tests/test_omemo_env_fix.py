"""Regression tests for the OMEMO enabled-flag (issue #5): ``XMPP_OMEMO_ENABLED``
must be honoured — False/0 disable, true/1 enable, yaml ``omemo.enabled``
honoured, unset defaults to true.

The original version of this file imported the REAL gateway package (and so
only ever ran with PYTHONPATH pointed at a hermes-agent checkout); it followed
none of the suite's conftest discipline and failed at collection everywhere
else. This rewrite keeps every test's intent but bootstraps the same fakes the
rest of the suite uses so it runs in a standalone plugin checkout.
"""
from __future__ import annotations

import sys
import unittest.mock
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, __file__.rsplit("/", 1)[0] + "/..")
sys.path.insert(0, __file__.rsplit("/", 1)[0])

from conftest import snapshot_sys_modules, restore_sys_modules  # noqa: E402

_snap = snapshot_sys_modules()

# --- fake gateway (so adapter.py imports in a standalone checkout) -----------

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
gw_base.MessageEvent = type("MessageEvent", (), {"__init__": lambda self, **kw: None})
gw_base.MessageType = type("MessageType", (), {"TEXT": "text", "IMAGE": "image", "COMMAND": "command"})
gw_base.ProcessingOutcome = type("ProcessingOutcome", (), {"SUCCESS": 0, "FAILURE": 1, "CANCELLED": 2})
gw_base.BasePlatformAdapter = type(
    "BasePlatformAdapter", (), {"__init__": lambda s, *a, **k: setattr(s, "config", a[0] if a else None)}
)

# --- fake agent.secret_scope (adapter resolves it lazily at call time) --------

_agent = unittest.mock.MagicMock()
sys.modules["agent"] = _agent
_ss = unittest.mock.MagicMock()
sys.modules["agent.secret_scope"] = _ss


def _fake_get_secret(name, default=None):
    # Neutral state: delegate to os.environ exactly like the real module does
    # when no scope is installed on a single-profile host.
    import os
    return os.environ.get(name, default)


def _fake_is_multiplex_active():
    return False


_ss.get_secret = _fake_get_secret
_ss.is_multiplex_active = _fake_is_multiplex_active

import adapter  # noqa: E402

restore_sys_modules(_snap)
# The agent fakes must stay installed: adapter imports agent.secret_scope
# LAZILY at call time, after restore ran (same pattern as
# test_multiplex_credentials.py). Neutral fake == module absent (env fallback).
sys.modules["agent"] = _agent
sys.modules["agent.secret_scope"] = _ss


def _clean_env(monkeypatch):
    for name in ("XMPP_OMEMO_ENABLED", "XMPP_JID", "XMPP_PASSWORD", "HERMES_HOME"):
        monkeypatch.delenv(name, raising=False)


def _make_cfg(**extra):
    cfg = MagicMock()
    # NOTE: no omemo_enabled here — extra would win over the env var, and
    # these tests exercise the env-var precedence level specifically.
    base = {"jid": "test@example.org", "password": "secret"}
    base.update(extra)
    cfg.extra = base
    return cfg


def _make_omemo_cfg(enabled):
    """Config whose extra carries an omemo sub-dict (as _apply_yaml_config builds)."""
    cfg = MagicMock()
    cfg.extra = {"jid": "test@example.org", "password": "secret", "omemo": {"enabled": enabled}}
    return cfg


# --- env-var flag ------------------------------------------------------------

def test_omemo_env_var_false_disabled(monkeypatch):
    """Regression for issue #5: XMPP_OMEMO_ENABLED=False must be honoured."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("XMPP_OMEMO_ENABLED", "False")
    a = adapter.XmppAdapter(_make_cfg())
    assert a._omemo_enabled is False, (
        f"Expected _omemo_enabled=False when XMPP_OMEMO_ENABLED=False, got {a._omemo_enabled}"
    )


def test_omemo_env_var_true_enabled(monkeypatch):
    """Sanity: true still works."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("XMPP_OMEMO_ENABLED", "true")
    a = adapter.XmppAdapter(_make_cfg())
    assert a._omemo_enabled is True


def test_omemo_env_var_zero_disabled(monkeypatch):
    """Numeric zero should also disable."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("XMPP_OMEMO_ENABLED", "0")
    a = adapter.XmppAdapter(_make_cfg())
    assert a._omemo_enabled is False


def test_omemo_env_var_one_enabled(monkeypatch):
    """Numeric one should enable."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("XMPP_OMEMO_ENABLED", "1")
    a = adapter.XmppAdapter(_make_cfg())
    assert a._omemo_enabled is True


# --- yaml omemo sub-config ---------------------------------------------------

def test_omemo_yaml_bool_true_enabled(monkeypatch):
    """YAML boolean true should enable."""
    _clean_env(monkeypatch)
    a = adapter.XmppAdapter(_make_omemo_cfg(True))
    assert a._omemo_enabled is True


def test_omemo_yaml_bool_false_disabled(monkeypatch):
    """YAML boolean false should disable."""
    _clean_env(monkeypatch)
    a = adapter.XmppAdapter(_make_omemo_cfg(False))
    assert a._omemo_enabled is False


def test_omemo_yaml_string_false_disabled(monkeypatch):
    """YAML string "false" should disable (via _truthy)."""
    _clean_env(monkeypatch)
    a = adapter.XmppAdapter(_make_omemo_cfg("false"))
    assert a._omemo_enabled is False


# --- default -----------------------------------------------------------------

def test_omemo_defaults_to_true_when_unset(monkeypatch):
    """If neither env nor yaml omemo_enabled is set, default to true."""
    _clean_env(monkeypatch)
    cfg = MagicMock()
    cfg.extra = {"jid": "test@example.org", "password": "secret"}
    a = adapter.XmppAdapter(cfg)
    assert a._omemo_enabled is True
