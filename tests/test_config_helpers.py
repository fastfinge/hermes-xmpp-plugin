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


def test_validate_config_accepts_extra_without_env(monkeypatch, tmp_path):
    monkeypatch.delenv("XMPP_JID", raising=False)
    monkeypatch.delenv("XMPP_PASSWORD", raising=False)
    # The credential probe may peek at $HERMES_HOME/.env (cron-preflight support),
    # so isolate from any developer-machine ~/.hermes/.env: this test asserts
    # config-extra-ONLY behaviour with no other credential source present.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter._DOTENV_PEEK_CACHE.clear()
    assert adapter.validate_config(Config({"jid": "bot@example.org", "password": "pw"}))
    assert not adapter.validate_config(Config({"jid": "bot@example.org"}))


# --- dotenv peek (cron delivery preflight support) -------------------------

def test_dotenv_peek_reads_env_file_without_export(monkeypatch, tmp_path):
    """The cron preflight runs before its dotenv pass: _dotenv_peek must find
    XMPP credentials in $HERMES_HOME/.env WITHOUT exporting them to os.environ."""
    monkeypatch.delenv("XMPP_JID", raising=False)
    monkeypatch.delenv("XMPP_PASSWORD", raising=False)
    (tmp_path / ".env").write_text("XMPP_JID=bot@example.org\nXMPP_PASSWORD=peek-pw\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter._DOTENV_PEEK_CACHE.clear()
    assert adapter._dotenv_peek("XMPP_JID") == "bot@example.org"
    assert adapter._dotenv_peek("XMPP_PASSWORD") == "peek-pw"
    # non-mutating: nothing leaked into the process environment
    assert "XMPP_JID" not in os.environ
    assert "XMPP_PASSWORD" not in os.environ
    # and the probe verdicts work with zero process env
    assert adapter.validate_config(Config({})) is True
    assert adapter.is_connected(Config({})) is True


def test_dotenv_peek_env_vars_win(monkeypatch, tmp_path):
    monkeypatch.setenv("XMPP_JID", "env-wins@example.org")
    monkeypatch.setenv("XMPP_PASSWORD", "env-pw")
    (tmp_path / ".env").write_text("XMPP_JID=file@example.org\nXMPP_PASSWORD=file-pw\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter._DOTENV_PEEK_CACHE.clear()
    jid, pw = adapter._xmpp_credentials(Config({}))
    assert (jid, pw) == ("env-wins@example.org", "env-pw")


def test_dotenv_peek_missing_file_is_false(monkeypatch, tmp_path):
    monkeypatch.delenv("XMPP_JID", raising=False)
    monkeypatch.delenv("XMPP_PASSWORD", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # no .env written
    adapter._DOTENV_PEEK_CACHE.clear()
    assert adapter._dotenv_peek("XMPP_JID") == ""
    assert adapter.validate_config(Config({})) is False
    assert adapter.is_connected(Config({})) is False
