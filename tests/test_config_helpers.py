from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import unittest.mock

# Mock gateway modules before importing adapter
for m in ["gateway", "gateway.config", "gateway.platforms", "gateway.platforms.base",
          "gateway.platforms.models", "gateway.util", "tools", "tools.clarify_gateway"]:
    sys.modules[m] = unittest.mock.MagicMock()

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import adapter  # noqa: E402


@pytest.fixture(autouse=True)
def _cleanup_adapter_module():
    """Remove adapter from sys.modules after each test to prevent caching a broken version."""
    yield
    for key in list(sys.modules.keys()):
        if key == "adapter" or key.startswith("adapter."):
            del sys.modules[key]


class Config:
    def __init__(self, extra=None):
        self.extra = extra or {}


def test_parse_muc_rooms_with_optional_nick():
    rooms = adapter._parse_muc_rooms("a@conference.example/herm,b@muc.example", "bot")
    assert [(r.room, r.nick) for r in rooms] == [
        ("a@conference.example", "herm"),
        ("b@muc.example", "bot"),
    ]


def test_apply_yaml_config_seeds_extra_and_env(monkeypatch):
    env_names = ["XMPP_JID", "XMPP_PASSWORD", "XMPP_ALLOWED_USERS", "XMPP_HOME_CHANNEL"]
    for name in env_names:
        monkeypatch.delenv(name, raising=False)
    extra = adapter._apply_yaml_config({}, {
        "jid": "bot@example.org",
        "password": "secret",
        "allowed_users": ["sam@example.org", "mom@example.org"],
        "home_channel": "sam@example.org",
    })
    assert extra["jid"] == "bot@example.org"
    assert os.environ["XMPP_JID"] == "bot@example.org"
    assert os.environ["XMPP_ALLOWED_USERS"] == "sam@example.org,mom@example.org"
    assert os.environ["XMPP_HOME_CHANNEL"] == "sam@example.org"
    # _apply_yaml_config sets these via raw os.environ[...] = ..., which
    # monkeypatch's own teardown does not know to revert (it only reverts
    # changes made through monkeypatch itself) — clean up explicitly so
    # these don't leak into other tests in the same process.
    for name in env_names:
        monkeypatch.delenv(name, raising=False)


def test_validate_config_accepts_extra_without_env(monkeypatch):
    monkeypatch.delenv("XMPP_JID", raising=False)
    monkeypatch.delenv("XMPP_PASSWORD", raising=False)
    assert adapter.validate_config(Config({"jid": "bot@example.org", "password": "pw"}))
    assert not adapter.validate_config(Config({"jid": "bot@example.org"}))
