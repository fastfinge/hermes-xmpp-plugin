"""Verified MUC-only grants must survive Hermes' second authorization gate."""
import ast
import asyncio
from enum import Enum
from types import MethodType, ModuleType, SimpleNamespace
from unittest.mock import MagicMock

from test_installed_voice_transport import SOURCE, real_core
from test_voice_notes import adapter, _adapter, msg, body, captured


def test_verified_muc_only_user_passes_real_core_gate_not_dm(real_core, monkeypatch):
    tree = ast.parse((SOURCE / "gateway/authz_mixin.py").read_text())
    names = {"_is_user_authorized", "_principal_authorized"}
    methods = [node for cls in tree.body if isinstance(cls, ast.ClassDef)
               for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in methods} == names

    class Platform(Enum):
        XMPP = "xmpp"
        WEBHOOK = "webhook"
        TELEGRAM = "telegram"

    local = ModuleType("gateway.session_local")
    setattr(local, "authorize_local_source", lambda *args: None)
    monkeypatch.setitem(__import__("sys").modules, "gateway.session_local", local)
    ns = {
        "SessionSource": object,
        "Platform": Platform,
        "_GROUP_CHAT_TYPES": {"group"},
        "_GROUP_FORUM_TYPES": {"group", "forum"},
        "_GROUP_USER_ENV": {},
        "_registry_entry": lambda _: SimpleNamespace(trusted_inbound=False),
        "authz_field": lambda platform, key, entry: {"allowed_users_env": "XMPP_ALLOWED_USERS"}.get(key),
        "_env_truthy": lambda _: False,
        "_auth_env": lambda key: "owner@example.org" if key == "XMPP_ALLOWED_USERS" else "",
        "_group_chat_env": lambda *args: "",
        "_coerce_allow_set": lambda value: set(value.split(",")) if value else set(),
        "_principal_matches_allowlist": lambda source, user, allowed: user in allowed,
    }
    module = ast.Module(body=[], type_ignores=[])
    module.body.extend(methods)
    exec(compile(module, "real-core-authz", "exec"), ns)
    runner = SimpleNamespace(
        _adapter_profile_for_source=lambda _: None,
        _chat_scoped_grant=lambda *args: False,
        _pairing_store_for=lambda _: None,
        _adapter_resolved_allowlist_ids=lambda _: set(),
    )
    runner._principal_authorized = MethodType(ns["_principal_authorized"], runner)
    authorized = MethodType(ns["_is_user_authorized"], runner)

    bot = _adapter(allowed_users=["owner@example.org"], muc_allowed_users=["tester@example.org"])
    bot.allow_all_users = False
    bot._known_mucs = {"room@conference.example.org"}
    bot.client = MagicMock()
    bot._registered_plugins = {"xep_0045"}
    bot.client["xep_0045"].get_jid_property.return_value = "tester@example.org/phone"
    captured.clear()
    asyncio.run(bot._on_message(msg(body("hello"), frm="room@conference.example.org/tester", mtype="groupchat")))
    assert len(captured) == 1
    source = captured[0].source
    source.platform = Platform.XMPP
    source.is_bot = False
    assert authorized(source), "core rejected the adapter-verified MUC-only sender"

    dm = SimpleNamespace(platform=Platform.XMPP, chat_type="dm", chat_id="tester@example.org",
                         user_id="tester@example.org", role_authorized=False, is_bot=False)
    assert not authorized(dm)
    captured.clear()
    asyncio.run(bot._on_message(msg(body("hello"), frm="tester@example.org/phone")))
    assert captured == []
