from __future__ import annotations

import importlib.util
import sys
import types
import unittest.mock
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _mock_gateway():
    """Inject gateway / tools stubs so adapter.py can import without the full framework."""
    # Evict anything a previously-imported test file left cached, so adapter
    # always re-executes against the fakes installed below (order-independence).
    for key in list(sys.modules.keys()):
        if key == "adapter" or key.startswith("adapter.") or key.startswith("gateway") \
                or key == "tools" or key.startswith("tools."):
            del sys.modules[key]
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
    gw_base.MessageType = type("MessageType", (), {"TEXT": "text"})
    gw_base.ProcessingOutcome = type("ProcessingOutcome", (), {"SUCCESS": 0, "FAILURE": 1, "CANCELLED": 2})
    gw_base.SendResult = type("SendResult", (), {"__init__": lambda s, **kw: s.__dict__.update(kw) or None})
    gw_base.BasePlatformAdapter = type("BasePlatformAdapter", (), {
        "__init__": lambda s, *a, **k: setattr(s, "config", a[0] if a else None) or None,
        "emit_message_raw": lambda *a, **kw: None,
        "on_processing_start": lambda *a, **kw: None,
        "on_processing_complete": lambda *a, **kw: None,
        "send": lambda *a, **kw: None,
        "build_source": lambda s, **kw: unittest.mock.MagicMock(**kw),
        "_mark_disconnected": lambda s: None,
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
    omemo_storage.Nothing = type("Nothing", (), {"__init__": lambda self, *a, **k: None})
    omemo_storage.Storage = _FakeStorage
    omemo_types = unittest.mock.MagicMock()
    sys.modules["omemo.types"] = omemo_types
    omemo_types.DeviceInformation = type("DeviceInformation", (), {})
    omemo_types.JSONType = type("JSONType", (), {})


def load_plugin_module():
    _mock_gateway()
    ns = types.ModuleType("hermes_plugins")
    ns.__path__ = []
    ns.__package__ = "hermes_plugins"
    sys.modules["hermes_plugins"] = ns
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.hermes_xmpp_plugin",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.hermes_xmpp_plugin"
    mod.__path__ = [str(ROOT)]
    sys.modules["hermes_plugins.hermes_xmpp_plugin"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _cleanup_modules(monkeypatch):
    """Prevent sys.modules pollution leaking to other test files."""
    original = dict(sys.modules)
    yield
    # After the test, remove any modules that weren't in the original set.
    for key in list(sys.modules.keys()):
        if key not in original:
            del sys.modules[key]
        else:
            sys.modules[key] = original[key]


class FakeContext:
    def __init__(self):
        self.calls = []

    def register_platform(self, **kwargs):
        self.calls.append(kwargs)


def test_user_plugin_loader_shape_registers_platform():
    mod = load_plugin_module()
    ctx = FakeContext()
    mod.register(ctx)
    assert len(ctx.calls) == 1
    entry = ctx.calls[0]
    assert entry["name"] == "xmpp"
    assert entry["label"] == "XMPP/Jabber"
    assert entry["required_env"] == ["XMPP_JID", "XMPP_PASSWORD"]
    assert entry["cron_deliver_env_var"] == "XMPP_HOME_CHANNEL"
    assert entry["allowed_users_env"] == "XMPP_ALLOWED_USERS"
    assert callable(entry["adapter_factory"])
    assert callable(entry["standalone_sender_fn"])
    assert callable(entry["apply_yaml_config_fn"])
