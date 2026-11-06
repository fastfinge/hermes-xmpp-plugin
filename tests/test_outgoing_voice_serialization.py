"""Outgoing wire XML, using real Slixmpp SFS generation and serialization."""
import hashlib
from unittest.mock import AsyncMock

import pytest
from slixmpp import ClientXMPP
from slixmpp.xmlstream import ET

from test_voice_notes import adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("recipient,mtype", [
    ("user@example.org", "chat"),
    ("room@conference.example.org", "groupchat"),
])
async def test_voice_wire_contains_sfs_and_matching_legacy_url(tmp_path, monkeypatch, recipient, mtype):
    audio = tmp_path / "voice.ogg"
    data = b"OggS synthetic serialization fixture, not playback audio"
    audio.write_bytes(data)
    url = "https://upload.example.org/voice.ogg?download=1&token=synthetic"
    client = ClientXMPP("hermes@example.org/test", "unused")
    for plugin in ("xep_0066", "xep_0363", "xep_0447"):
        client.register_plugin(plugin)
    upload = AsyncMock(return_value=url)
    monkeypatch.setattr(client["xep_0363"], "upload_file", upload)
    sent = []
    # Capture stream boundary; never connect or replace Message/SFS objects.
    monkeypatch.setattr(client, "send", lambda stanza, **kwargs: sent.append(str(stanza)))
    bot = object.__new__(adapter.XmppAdapter)
    bot.client = client
    bot._running = True
    bot._registered_plugins = {"xep_0066", "xep_0363", "xep_0447"}
    bot._known_mucs = {"room@conference.example.org"}
    bot._known_dms = {"user@example.org"}

    result = await bot.send_voice(chat_id=recipient, audio_path=str(audio))

    assert result.success
    upload.assert_awaited_once_with(filename=str(audio), content_type="audio/ogg")
    assert len(sent) == 1
    # Slixmpp omits jabber:client namespace inherited from the open stream.
    stream = ET.fromstring('<stream xmlns="jabber:client">' + sent[0] + '</stream>')
    wire = stream[0]
    assert wire.get("to") == recipient and wire.get("type") == mtype
    shares = wire.findall("{urn:xmpp:sfs:0}file-sharing")
    assert len(shares) == 1
    share = shares[0]
    file = share.find("{urn:xmpp:file:metadata:0}file")
    assert file is not None, "SFS payload lost during stanza assignment"
    assert share.get("disposition") == "inline"
    ns = "{urn:xmpp:file:metadata:0}"
    assert file.findtext(ns + "media-type") == "audio/ogg"
    assert file.findtext(ns + "name") == audio.name
    assert file.findtext(ns + "size") == str(len(data))
    assert file.findtext(ns + "desc") == "Voice message"
    digest = file.find("{urn:xmpp:hashes:2}hash")
    assert digest is not None and digest.text
    import base64
    assert base64.b64decode(digest.text) == hashlib.new(digest.get("algo"), data).digest()
    source = share.find("{urn:xmpp:sfs:0}sources/{http://jabber.org/protocol/url-data}url-data")
    assert source is not None and source.get("target") == url
    assert wire.findtext("{jabber:client}body") == url
    assert wire.findtext("{jabber:x:oob}x/{jabber:x:oob}url") == url
    fallbacks = wire.findall("{urn:xmpp:fallback:0}fallback")
    assert len(fallbacks) == 1
    fallback = fallbacks[0]
    assert fallback.get("for") == "urn:xmpp:sfs:0"
    bodies = fallback.findall("{urn:xmpp:fallback:0}body")
    assert len(bodies) == 1 and not bodies[0].attrib
