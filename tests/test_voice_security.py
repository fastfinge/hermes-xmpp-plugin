"""Authorization and encrypted attachment failure regressions; no live accounts."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from slixmpp.jid import JID

from test_voice_notes import adapter, _adapter, msg, body, oob, captured, core


@pytest.mark.parametrize("kind", ["text", "voice", "clarify"])
@pytest.mark.parametrize("identity", [None, "observer@example.org", "owner@example.org/phone"])
def test_muc_sender_gate_before_any_side_effect(kind, identity, core, monkeypatch):
    a = _adapter(muc_allowed_users="owner@example.org")
    a._known_mucs = {"room@conference.example.org"}
    a.client = MagicMock()
    a._registered_plugins = {"xep_0045"}
    a.client["xep_0045"].get_jid_property.return_value = JID(identity) if identity else None
    download = AsyncMock(return_value="/fake/audio.ogg")
    form = MagicMock(return_value=True)
    monkeypatch.setattr(a, "_cache_voice_attachment", download)
    monkeypatch.setattr(a, "_handle_form_submission", form)
    url = "https://upload.example.org/a.ogg"
    inner = {"text": body("hello"), "voice": body(url) + oob(url),
             "clarify": "<x xmlns='jabber:x:data' type='submit'/>"}[kind]
    # Forged message identity must never override server presence cache.
    inner += "<x xmlns='http://jabber.org/protocol/muc#user'><item jid='owner@example.org'/></x>"
    from test_voice_notes import _XMPP
    _XMPP.register_plugin("xep_0004")
    stanza = msg(inner, frm="room@conference.example.org/owner", mtype="groupchat")
    captured.clear()
    if identity == "owner@example.org/phone":
        if kind == "clarify":
            stanza.stream.register_plugin("xep_0004")
        asyncio.run(a._on_message(stanza))
        if kind == "clarify":
            form.assert_called_once()
            assert captured == []
        else:
            assert len(captured) == 1
            assert captured[0].source.user_id == "owner@example.org"
            if kind == "voice":
                download.assert_awaited_once()
    else:
        asyncio.run(a._on_message(stanza))
        assert captured == []
        download.assert_not_called()
        form.assert_not_called()
        assert core.log == []


def test_unauthorized_muc_never_decrypts(core, monkeypatch):
    a = _adapter(muc_allowed_users=["owner@example.org"])
    a._known_mucs = {"room@conference.example.org"}
    a.client = MagicMock()
    a._registered_plugins = {"xep_0045", "xep_0384"}
    a.client["xep_0045"].get_jid_property.return_value = "observer@example.org/phone"
    decrypt = AsyncMock()
    a.client["xep_0384"].is_encrypted.return_value = True
    a.client["xep_0384"].decrypt_message = decrypt
    monkeypatch.setattr(adapter, "SLIXMPP_OMEMO_AVAILABLE", True)
    captured.clear()
    asyncio.run(a._on_message(msg(body("encrypted"), frm="room@conference.example.org/observer", mtype="groupchat")))
    decrypt.assert_not_awaited()
    assert captured == [] and core.log == []


def test_muc_allowlist_env_and_yaml(monkeypatch):
    monkeypatch.setenv("XMPP_MUC_ALLOWED_USERS", "tester@example.org/mobile")
    assert _adapter().muc_allowed_users == {"tester@example.org"}
    extra = adapter._apply_yaml_config({}, {"muc_allowed_users": ["owner@example.org"]})
    assert extra["muc_allowed_users"] == ["owner@example.org"]
    assert _adapter(**extra).muc_allowed_users == {"owner@example.org"}


def test_muc_room_and_sender_lists_are_independent():
    a = _adapter(allowed_users="owner@example.org")
    a._known_mucs = {"room@conference.example.org"}
    assert a.muc_allowed_users == {"owner@example.org"}
    assert a._is_authorized(chat_type="group", chat_id="room@conference.example.org", user_jid="owner@example.org/mobile")
    assert not a._is_authorized(chat_type="group", chat_id="other@conference.example.org", user_jid="owner@example.org")
    assert not a._is_authorized(chat_type="group", chat_id="room@conference.example.org", user_jid="")
    assert _adapter(allowed_users="owner@example.org", muc_allowed_users=[]).muc_allowed_users == set()


def test_yaml_list_default_authorizes_dm_and_muc_owner():
    a = _adapter(allowed_users=["owner@example.org"])
    a.allow_all_users = False
    a._known_mucs = {"room@conference.example.org"}
    assert a.allowed_users == {"owner@example.org"}
    assert a.muc_allowed_users == {"owner@example.org"}
    assert a._is_authorized(chat_type="dm", chat_id="owner@example.org", user_jid="owner@example.org/phone")
    assert a._is_authorized(chat_type="group", chat_id="room@conference.example.org", user_jid="owner@example.org/phone")


@pytest.mark.parametrize("failure", ["download", "decrypt", "untrusted", "malformed"])
def test_encrypted_failure_never_dispatched_or_logged(failure, core, monkeypatch, caplog):
    a = _adapter()
    fragment = "ab" * 44
    url = "aesgcm://upload.example.org/a.ogg#" + fragment
    if failure == "malformed":
        url = "aesgcm://upload.example.org/a.ogg#bad-secret"
    elif failure == "untrusted":
        a._voice_trusted_hosts = ()
        core.safe = lambda _: False
    elif failure == "decrypt":
        core.routes["https://upload.example.org/a.ogg"] = (200, b"invalid ciphertext")
    else:
        async def fail(*args, **kwargs):
            raise ValueError(url)
        monkeypatch.setattr(adapter, "_fetch_voice_bytes", fail)
    captured.clear()
    asyncio.run(a._on_message(msg(body(url))))
    assert captured == []
    assert fragment not in caplog.text
    assert "bad-secret" not in caplog.text
    assert "aesgcm://" not in caplog.text


@pytest.mark.parametrize("strip_mode", ["none", "whole", "scheme"])
def test_encrypted_voice_reply_never_dispatches_key(strip_mode, core, caplog):
    from test_voice_notes import _XMPP, _aesgcm

    _XMPP.register_plugin("xep_0461")
    a = _adapter()
    a.client = _XMPP  # Exercise real XEP-0461 reply extraction.
    plaintext = b"OggS-reply-voice"
    ciphertext, fragment = _aesgcm(plaintext)
    core.routes["https://upload.example.org/reply.ogg"] = (200, ciphertext)
    url = "aesgcm://upload.example.org/reply.ogg#" + fragment
    inner = body(url) + "<reply xmlns='urn:xmpp:reply:0' id='previous-message'/>"
    end = len(url) if strip_mode == "whole" else len("aesgcm://")
    if strip_mode != "none":
        inner += (
            "<fallback xmlns='urn:xmpp:fallback:0' for='urn:xmpp:reply:0'>"
            f"<body start='0' end='{end}'/></fallback>"
        )
    stanza = msg(inner)
    assert stanza["reply"].strip_fallback_content() == (url[end:] if strip_mode != "none" else url)
    captured.clear()
    asyncio.run(a._on_message(stanza))
    assert core.cached == [(plaintext, ".ogg")]
    assert len(captured) == 1
    event = captured[0]
    assert event.message_type == "voice"
    assert event.media_urls == ["/hermes/cache/audio/audio_1.ogg"]
    assert event.reply_to_message_id == "previous-message"
    assert event.text == ""
    assert event.reply_to_text is None
    for text in (event.text, event.reply_to_text or "", caplog.text):
        assert url not in text
        assert fragment not in text
        assert "aesgcm://" not in text.lower()


def test_disabled_stt_never_dispatches_encrypted_key(core):
    a = _adapter(voice_transcription=False)
    captured.clear()
    asyncio.run(a._on_message(msg(body("aesgcm://upload.example.org/a.bin#private-key"))))
    assert captured == []
    assert core.log == []
