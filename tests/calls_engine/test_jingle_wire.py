"""Parse the real Dino and Conversations offers, and round-trip them through build()."""

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from calls.jingle import wire
from calls.jingle.model import JINGLE

FIXTURES = Path(__file__).parent / "captures"


def load(name: str) -> wire.Jingle:
    element = ET.fromstring((FIXTURES / name).read_text()).find(f"{{{JINGLE}}}jingle")
    assert element is not None
    return wire.parse(element)


def test_conversations_offer() -> None:
    offer = load("conversations-session-initiate.xml")
    assert offer.action == "session-initiate"
    assert offer.group == ("0",)
    (content,) = offer.contents
    assert content.media == "audio"
    assert content.rtcp_mux
    assert {p.name for p in content.payloads} >= {"opus", "red", "telephone-event"}
    opus = next(p for p in content.payloads if p.name == "opus")
    assert (opus.id, opus.clockrate, opus.channels) == (111, 48000, 2)
    assert ("useinbandfec", "1") in opus.parameters
    assert len(content.header_extensions) == 4
    assert content.transport is not None
    assert content.transport.fingerprint is not None
    assert content.transport.fingerprint.setup == "actpass"
    assert content.transport.fingerprint.hash == "sha-256"


def test_dino_offer() -> None:
    offer = load("dino-session-initiate.xml")
    assert offer.initiator is not None
    assert offer.initiator.endswith("/dino.f2292a9f")
    assert offer.group == ()
    (content,) = offer.contents
    assert content.name == "audio"
    assert [p.id for p in content.payloads][:1] == [111]
    assert content.transport is not None
    assert {c.component for c in content.transport.candidates} == {1, 2}
    assert content.transport.fingerprint is not None


def test_dino_relay_candidate() -> None:
    update = load("dino-transport-info-relay.xml")
    (candidate,) = update.contents[0].transport.candidates  # type: ignore[union-attr]
    assert candidate.type == "relay"
    assert candidate.rel_port == 37548


@pytest.mark.parametrize(
    "name",
    [
        "conversations-session-initiate.xml",
        "dino-session-initiate.xml",
        "dino-transport-info-relay.xml",
    ],
)
def test_round_trip(name: str) -> None:
    original = load(name)
    again = wire.parse(ET.fromstring(ET.tostring(wire.build(original))))
    assert again == original


def test_decline_is_namespaced() -> None:
    xml = ET.tostring(wire.build(wire.Jingle("session-terminate", "s1", reason="decline"))).decode()
    assert 'xmlns=""' not in xml
    assert wire.parse(ET.fromstring(xml)).reason == "decline"
