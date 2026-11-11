"""Jingle to SDP against the real client offers, checked with aiortc's own SDP parser."""

from dataclasses import replace
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pytest
from aiortc.sdp import SessionDescription

from calls.jingle import sdp, wire
from calls.jingle.model import (
    JINGLE,
    Fingerprint,
    Jingle,
    JingleError,
    SecurityError,
    UnsupportedError,
)

FIXTURES = Path(__file__).parent / "captures"
FINGERPRINT = ":".join(["AB"] * 32)


def load(name: str) -> Jingle:
    element = ET.fromstring((FIXTURES / name).read_text()).find(f"{{{JINGLE}}}jingle")
    assert element is not None
    offer = wire.parse(element)
    # Fixtures are redacted ("X"); give the content a syntactically valid fingerprint.
    (content,) = offer.contents
    assert content.transport is not None
    # ... and the redacted candidate addresses a real one.
    candidates = tuple(replace(c, ip="192.168.1.5") for c in content.transport.candidates)
    transport = replace(
        content.transport,
        fingerprint=Fingerprint("sha-256", "actpass", FINGERPRINT),
        candidates=candidates,
    )
    return replace(offer, contents=(replace(content, transport=transport),))


@pytest.mark.parametrize(
    "name", ["conversations-session-initiate.xml", "dino-session-initiate.xml"]
)
def test_offer_parses_in_aiortc(name: str) -> None:
    offer = load(name)
    parsed = SessionDescription.parse(sdp.offer_to_sdp(offer))
    (media,) = parsed.media
    assert media.kind == "audio"
    assert [c.name for c in media.rtp.codecs] == ["opus"]
    assert media.dtls is not None
    assert media.dtls.fingerprints[0].algorithm == "sha-256"
    assert media.ice is not None
    assert media.rtcp_mux


def test_dino_drops_component_2_and_sdes() -> None:
    text = sdp.offer_to_sdp(load("dino-session-initiate.xml"))
    assert text.count("a=candidate:") == 4
    assert "crypto" not in text
    assert "a=mid:audio" in text
    assert "BUNDLE" not in text


def test_conversations_keeps_bundle_and_name() -> None:
    text = sdp.offer_to_sdp(load("conversations-session-initiate.xml"))
    assert "a=group:BUNDLE 0" in text
    assert "a=mid:0" in text
    assert "useinbandfec=1" in text


def test_refuses_unencrypted_offer() -> None:
    offer = load("dino-session-initiate.xml")
    (content,) = offer.contents
    assert content.transport is not None
    bare = replace(content, transport=replace(content.transport, fingerprint=None))
    with pytest.raises(SecurityError):
        sdp.offer_to_sdp(replace(offer, contents=(bare,)))


def test_refuses_offer_without_opus() -> None:
    offer = load("dino-session-initiate.xml")
    (content,) = offer.contents
    no_opus = replace(content, payloads=tuple(p for p in content.payloads if p.name != "opus"))
    with pytest.raises(UnsupportedError):
        sdp.offer_to_sdp(replace(offer, contents=(no_opus,)))


def test_candidate_round_trip() -> None:
    relay = load("dino-transport-info-relay.xml").contents[0].transport
    assert relay is not None
    (candidate,) = relay.candidates
    again = sdp.candidate_from_sdp(sdp.candidate_to_sdp(candidate))
    assert (again.ip, again.port, again.type, again.rel_port) == (
        candidate.ip,
        candidate.port,
        candidate.type,
        candidate.rel_port,
    )


ANSWER = "\r\n".join(
    [
        "v=0",
        "m=audio 9 UDP/TLS/RTP/SAVPF 111",
        "a=ice-ufrag:abcd",
        "a=ice-pwd:0123456789012345678901",
        f"a=fingerprint:sha-256 {FINGERPRINT}",
        "a=setup:active",
        "a=rtpmap:111 opus/48000/2",
        "a=fmtp:111 minptime=10;useinbandfec=1",
        "a=candidate:1 1 udp 2113937151 100.64.0.1 40000 typ host",
        "a=candidate:1 2 udp 2113937150 100.64.0.1 40001 typ host",
    ]
)


def test_answer_to_accept_content() -> None:
    offer = load("conversations-session-initiate.xml")
    content = sdp.answer_to_content(ANSWER, offer.contents[0])
    assert (content.name, content.creator) == ("0", "initiator")
    assert content.payloads[0].parameters == (("minptime", "10"), ("useinbandfec", "1"))
    assert content.transport is not None
    assert content.transport.fingerprint is not None
    assert content.transport.fingerprint.setup == "active"
    assert len(content.transport.candidates) == 1
    accept = Jingle("session-accept", offer.sid, contents=(content,), group=offer.group)
    again = wire.parse(ET.fromstring(ET.tostring(wire.build(accept))))
    assert again == accept


def _with(offer: Jingle, **changes: Any) -> Jingle:
    (content,) = offer.contents
    assert content.transport is not None
    return replace(
        offer, contents=(replace(content, transport=replace(content.transport, **changes)),)
    )


def test_a_newline_smuggled_into_an_attribute_is_refused() -> None:
    offer = load("conversations-session-initiate.xml")
    for field in ("ufrag", "pwd"):
        with pytest.raises(JingleError):
            sdp.offer_to_sdp(_with(offer, **{field: "abc\r\nm=video 9 RTP/AVP 96"}))


def test_candidates_must_be_real_udp_addresses() -> None:
    offer = load("dino-session-initiate.xml")
    (content,) = offer.contents
    assert content.transport is not None
    first = content.transport.candidates[0]
    for bad in (
        replace(first, ip="127.0.0.1"),
        replace(first, ip="169.254.169.254"),
        replace(first, ip="::1"),
        replace(first, ip="224.0.0.1"),
        replace(first, ip="victim.local"),
        replace(first, port=22),
        replace(first, protocol="tcp"),
    ):
        assert not sdp.acceptable_candidate(bad), bad
    for good in (replace(first, ip="100.64.0.7"), replace(first, ip="203.0.113.9")):
        assert sdp.acceptable_candidate(good), good


def test_a_flood_of_candidates_is_capped() -> None:
    offer = load("dino-session-initiate.xml")
    (content,) = offer.contents
    assert content.transport is not None
    many = tuple(replace(content.transport.candidates[0], id=str(i)) for i in range(500))
    text = sdp.offer_to_sdp(_with(offer, candidates=many))
    assert text.count("a=candidate:") == sdp.MAX_CANDIDATES


def test_boolean_fmtp_values_become_numbers_aiortc_accepts() -> None:
    """Monal sends Opus flags as true/false; aiortc's parser fails on int('true')."""
    offer = load("conversations-session-initiate.xml")
    (content,) = offer.contents
    opus = next(p for p in content.payloads if p.name.lower() == "opus")
    flagged = replace(
        opus, parameters=(("useinbandfec", "true"), ("usedtx", "False"), ("minptime", "10"))
    )
    payloads = tuple(flagged if p is opus else p for p in content.payloads)
    text = sdp.offer_to_sdp(replace(offer, contents=(replace(content, payloads=payloads),)))
    assert "useinbandfec=1;usedtx=0;minptime=10" in text
    SessionDescription.parse(text)  # the real aiortc parser used to raise ValueError here


@pytest.mark.parametrize("name", ["x=1;stereo", "", "a b", "\uff58"])
def test_fmtp_names_that_could_split_or_inject_are_refused(name: str) -> None:
    offer = load("conversations-session-initiate.xml")
    (content,) = offer.contents
    opus = next(p for p in content.payloads if p.name.lower() == "opus")
    bad = replace(opus, parameters=((name, "1"),))
    payloads = tuple(bad if p is opus else p for p in content.payloads)
    with pytest.raises(JingleError):
        sdp.offer_to_sdp(replace(offer, contents=(replace(content, payloads=payloads),)))
