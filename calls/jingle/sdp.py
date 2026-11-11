"""Jingle audio content to SDP and back.

Scope is one audio m-line, Opus only, DTLS-SRTP only, rtcp-mux. The legacy SDES
`<encryption>` block, RTCP component 2 candidates and every codec but Opus are dropped
(Dino offers speex/PCMU/PCMA and SDES; Conversations offers red and telephone-event).
"""

import ipaddress
import re

from .model import (
    Candidate,
    Content,
    Fingerprint,
    Jingle,
    JingleError,
    PayloadType,
    SecurityError,
    Transport,
    UnsupportedError,
)


def offer_to_sdp(offer: Jingle) -> str:
    """Build the SDP offer to hand to the WebRTC stack from a Jingle `session-initiate`."""
    content = next((c for c in offer.contents if c.media == "audio"), None)
    if content is None:
        raise UnsupportedError("no audio content in offer")
    transport = content.transport
    if transport is None or transport.fingerprint is None:
        raise SecurityError("offer has no DTLS fingerprint; unencrypted calls are refused")
    opus = next((p for p in content.payloads if p.name.lower() == "opus"), None)
    if opus is None:
        raise UnsupportedError("offer has no Opus payload")
    lines = ["v=0", "o=- 1 2 IN IP4 127.0.0.1", "s=-", "t=0 0"]
    if offer.group:
        lines.append("a=group:BUNDLE " + " ".join(map(_safe, offer.group)))
    lines += [
        f"m=audio 9 UDP/TLS/RTP/SAVPF {opus.id}",
        "c=IN IP4 0.0.0.0",
        "a=rtcp:9 IN IP4 0.0.0.0",
        f"a=ice-ufrag:{_safe(transport.ufrag)}",
        f"a=ice-pwd:{_safe(transport.pwd)}",
        f"a=fingerprint:{_safe(transport.fingerprint.hash)} {_safe(transport.fingerprint.value)}",
        f"a=setup:{_safe(transport.fingerprint.setup)}",
        f"a=mid:{_safe(content.name)}",
        "a=sendrecv",
        "a=rtcp-mux",
        f"a=rtpmap:{_safe(opus.id)} opus/{_safe(opus.clockrate)}/{_safe(opus.channels)}",
    ]
    if opus.parameters:
        lines.append(
            f"a=fmtp:{_safe(opus.id)} "
            + ";".join(f"{_fmtp_name(k)}={_fmtp_value(v)}" for k, v in opus.parameters)
        )
    lines += [
        f"a=candidate:{candidate_to_sdp(c)}"
        for c in transport.candidates[:MAX_CANDIDATES]
        if c.component == 1 and acceptable_candidate(c)
    ]
    return "\r\n".join(lines) + "\r\n"


_SAFE = re.compile(r"[\w+/:=.,;-]*", re.ASCII)
_PARAM = re.compile(r"[\w+/:.,-]+", re.ASCII)  # one fmtp name or value: no `;` or `=` to split on
MAX_CANDIDATES = 16  # per call: a peer cannot make us probe thousands of addresses


def _safe(value: object) -> str:
    """A value for an SDP line must have no whitespace or control characters: XML character
    references can smuggle a newline, and with it a whole extra line, into an attribute."""
    text = str(value)
    if not _SAFE.fullmatch(text):
        raise JingleError(f"unsafe character in {text[:40]!r}")
    return text


def _fmtp_name(name: object) -> str:
    text = _safe(name)
    if not _PARAM.fullmatch(text):
        raise JingleError(f"unsafe fmtp name {text[:40]!r}")
    return text


def _fmtp_value(value: object) -> str:
    """Monal writes Opus flags as true/false; SDP and aiortc's parser want 1/0."""
    text = _safe(value)
    if text and not _PARAM.fullmatch(text):
        raise JingleError(f"unsafe fmtp value {text[:40]!r}")
    return {"true": "1", "false": "0"}.get(text.lower(), text)


def acceptable_candidate(c: Candidate) -> bool:
    """Only UDP to a real unicast address on an unprivileged port: a peer must not aim our
    connectivity checks at localhost, link-local (cloud metadata), multicast or hostnames."""
    try:
        ip = ipaddress.ip_address(c.ip)
    except ValueError:
        return False
    bad = (
        ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved
    )
    return c.protocol == "udp" and not bad and 1024 <= c.port <= 65535


def candidate_to_sdp(c: Candidate) -> str:
    """The value after `a=candidate:` (also the string for a trickled candidate)."""
    fields = (c.foundation, c.component, c.protocol, c.priority, c.ip, c.port, c.type)
    line = "{} {} {} {} {} {} typ {}".format(*map(_safe, fields))
    if c.rel_addr is not None and c.rel_port is not None:
        line += f" raddr {_safe(c.rel_addr)} rport {_safe(c.rel_port)}"
    return line


def candidate_from_sdp(value: str, generation: int = 0) -> Candidate:
    """Parse `foundation component proto priority ip port typ type [raddr x rport y]`."""
    try:
        return _candidate_from_sdp(value, generation)
    except (ValueError, IndexError) as error:
        raise JingleError(f"bad candidate: {value[:80]!r}") from error


def _candidate_from_sdp(value: str, generation: int) -> Candidate:
    parts = value.removeprefix("candidate:").split()
    if len(parts) < 8 or parts[6] != "typ":
        raise JingleError(f"bad candidate: {value[:80]!r}")
    extra = dict(zip(parts[8::2], parts[9::2], strict=False))
    return Candidate(
        foundation=parts[0],
        component=int(parts[1]),
        protocol=parts[2].lower(),
        priority=int(parts[3]),
        ip=parts[4],
        port=int(parts[5]),
        type=parts[7],
        generation=generation,
        id=f"{parts[0]}-{parts[1]}-{parts[4]}-{parts[5]}",
        rel_addr=extra.get("raddr"),
        rel_port=int(extra["rport"]) if "rport" in extra else None,
    )


def answer_to_content(sdp: str, offered: Content) -> Content:
    """Turn the stack's SDP answer into the Jingle content for `session-accept`.

    Mirrors the offer's `name`/`creator` so the content matches (Conversations uses name "0").
    """
    attrs: dict[str, str] = {}
    candidates: list[Candidate] = []
    opus: PayloadType | None = None
    fmtp: dict[int, tuple[tuple[str, str], ...]] = {}
    rtpmap: dict[int, tuple[str, int, int]] = {}
    for raw in sdp.replace("\r\n", "\n").split("\n"):
        if not raw.startswith("a="):
            continue
        key, _, value = raw[2:].partition(":")
        if key == "candidate":
            c = candidate_from_sdp(value)
            if c.component == 1:
                candidates.append(c)
        elif key == "rtpmap":
            number, _, spec = value.partition(" ")
            name, _, rest = spec.partition("/")
            rate, _, channels = rest.partition("/")
            rtpmap[int(number)] = (name, int(rate), int(channels or 1))
        elif key == "fmtp":
            number, _, spec = value.partition(" ")
            fmtp[int(number)] = tuple(
                (k, v) for k, _, v in (item.strip().partition("=") for item in spec.split(";")) if k
            )
        else:
            attrs.setdefault(key, value)
    for number, (name, rate, channels) in rtpmap.items():
        if name.lower() == "opus":
            opus = PayloadType(number, "opus", rate, channels, fmtp.get(number, ()))
    if opus is None:
        raise UnsupportedError("answer has no Opus payload")
    fingerprint_hash, _, fingerprint_value = attrs.get("fingerprint", "").partition(" ")
    if not fingerprint_value:
        raise SecurityError("answer has no DTLS fingerprint")
    return Content(
        creator=offered.creator,
        name=offered.name,
        senders="both",
        media="audio",
        payloads=(opus,),
        rtcp_mux=True,
        transport=Transport(
            ufrag=attrs.get("ice-ufrag", ""),
            pwd=attrs.get("ice-pwd", ""),
            fingerprint=Fingerprint(
                fingerprint_hash, attrs.get("setup", "active"), fingerprint_value
            ),
            candidates=tuple(candidates),
        ),
    )
