"""Jingle XML to model and back. Every element is built with an explicit namespace."""

from xml.etree import ElementTree as ET

from .model import (
    DTLS,
    GROUPING,
    HDREXT,
    ICE_UDP,
    JINGLE,
    RTCP_FB,
    RTP,
    RTP_INFO,
    Candidate,
    Content,
    Fingerprint,
    Jingle,
    JingleError,
    PayloadType,
    Transport,
)


def _q(namespace: str, tag: str) -> str:
    return f"{{{namespace}}}{tag}"


def parse(element: ET.Element) -> Jingle:
    """Parse a `<jingle xmlns='urn:xmpp:jingle:1'>` element. Unknown children are ignored.
    Malformed values (a peer's input) raise JingleError, never a bare ValueError."""
    try:
        return _parse(element)
    except (ValueError, TypeError) as error:
        raise JingleError(f"malformed jingle element: {error}") from error


def _parse(element: ET.Element) -> Jingle:
    if element.tag != _q(JINGLE, "jingle"):
        raise ValueError(f"not a jingle element: {element.tag}")
    group = tuple(
        c.get("name", "")
        for g in element.findall(_q(GROUPING, "group"))
        if g.get("semantics") == "BUNDLE"
        for c in g.findall(_q(GROUPING, "content"))
    )
    reason = element.find(_q(JINGLE, "reason"))
    reason_name = (
        next((child.tag.rsplit("}", 1)[-1] for child in reason), None)
        if reason is not None
        else None
    )
    info = next(
        (c.tag.rsplit("}", 1)[-1] for c in element if c.tag.startswith(f"{{{RTP_INFO}}}")), None
    )
    return Jingle(
        action=element.get("action", ""),
        sid=element.get("sid", ""),
        initiator=element.get("initiator"),
        contents=tuple(_parse_content(c) for c in element.findall(_q(JINGLE, "content"))),
        group=group,
        reason=reason_name,
        info=info,
    )


def _parse_content(element: ET.Element) -> Content:
    description = element.find(_q(RTP, "description"))
    transport = element.find(_q(ICE_UDP, "transport"))
    payloads: tuple[PayloadType, ...] = ()
    extensions: tuple[tuple[int, str], ...] = ()
    mux = False
    if description is not None:
        payloads = tuple(_parse_payload(p) for p in description.findall(_q(RTP, "payload-type")))
        extensions = tuple(
            (int(h.get("id", "0")), h.get("uri", ""))
            for h in description.findall(_q(HDREXT, "rtp-hdrext"))
        )
        mux = description.find(_q(RTP, "rtcp-mux")) is not None
    return Content(
        creator=element.get("creator", "initiator"),
        name=element.get("name", ""),
        senders=element.get("senders", "both"),
        media=description.get("media") if description is not None else None,
        payloads=payloads,
        header_extensions=extensions,
        rtcp_mux=mux,
        transport=_parse_transport(transport) if transport is not None else None,
    )


def _parse_payload(element: ET.Element) -> PayloadType:
    return PayloadType(
        id=int(element.get("id", "0")),
        name=element.get("name", ""),
        clockrate=int(element.get("clockrate", "0")),
        channels=int(element.get("channels", "1")),
        parameters=tuple(
            (p.get("name", ""), p.get("value", "")) for p in element.findall(_q(RTP, "parameter"))
        ),
        rtcp_fb=tuple(
            (f.get("type", ""), f.get("subtype")) for f in element.findall(_q(RTCP_FB, "rtcp-fb"))
        ),
    )


def _parse_transport(element: ET.Element) -> Transport:
    fingerprint = element.find(_q(DTLS, "fingerprint"))
    return Transport(
        ufrag=element.get("ufrag", ""),
        pwd=element.get("pwd", ""),
        fingerprint=(
            Fingerprint(
                fingerprint.get("hash", ""),
                fingerprint.get("setup", ""),
                (fingerprint.text or "").strip(),
            )
            if fingerprint is not None
            else None
        ),
        candidates=tuple(_parse_candidate(c) for c in element.findall(_q(ICE_UDP, "candidate"))),
    )


def _parse_candidate(element: ET.Element) -> Candidate:
    rel_port = element.get("rel-port")
    return Candidate(
        foundation=element.get("foundation", ""),
        component=int(element.get("component", "1")),
        protocol=element.get("protocol", "udp"),
        priority=int(element.get("priority", "0")),
        ip=element.get("ip", ""),
        port=int(element.get("port", "0")),
        type=element.get("type", "host"),
        generation=int(element.get("generation", "0")),
        id=element.get("id", ""),
        rel_addr=element.get("rel-addr"),
        rel_port=int(rel_port) if rel_port is not None else None,
    )


def build(jingle: Jingle) -> ET.Element:
    """Build a `<jingle>` element from the model."""
    root = ET.Element(_q(JINGLE, "jingle"), action=jingle.action, sid=jingle.sid)
    if jingle.initiator:
        root.set("initiator", jingle.initiator)
    for content in jingle.contents:
        root.append(_build_content(content))
    if jingle.group:
        group = ET.SubElement(root, _q(GROUPING, "group"), semantics="BUNDLE")
        for name in jingle.group:
            ET.SubElement(group, _q(GROUPING, "content"), name=name)
    if jingle.reason:
        ET.SubElement(ET.SubElement(root, _q(JINGLE, "reason")), _q(JINGLE, jingle.reason))
    return root


def _build_content(content: Content) -> ET.Element:
    element = ET.Element(_q(JINGLE, "content"), creator=content.creator, name=content.name)
    if content.senders != "both":
        element.set("senders", content.senders)
    if content.media is not None:
        description = ET.SubElement(element, _q(RTP, "description"), media=content.media)
        for payload in content.payloads:
            description.append(_build_payload(payload))
        for ext_id, uri in content.header_extensions:
            ET.SubElement(description, _q(HDREXT, "rtp-hdrext"), id=str(ext_id), uri=uri)
        if content.rtcp_mux:
            ET.SubElement(description, _q(RTP, "rtcp-mux"))
    if content.transport is not None:
        element.append(_build_transport(content.transport))
    return element


def _build_payload(payload: PayloadType) -> ET.Element:
    element = ET.Element(
        _q(RTP, "payload-type"),
        id=str(payload.id),
        name=payload.name,
        clockrate=str(payload.clockrate),
        channels=str(payload.channels),
    )
    for name, value in payload.parameters:
        ET.SubElement(element, _q(RTP, "parameter"), name=name, value=value)
    for kind, subtype in payload.rtcp_fb:
        feedback = ET.SubElement(element, _q(RTCP_FB, "rtcp-fb"), type=kind)
        if subtype is not None:
            feedback.set("subtype", subtype)
    return element


def _build_transport(transport: Transport) -> ET.Element:
    element = ET.Element(_q(ICE_UDP, "transport"), ufrag=transport.ufrag, pwd=transport.pwd)
    if transport.fingerprint is not None:
        fingerprint = ET.SubElement(
            element,
            _q(DTLS, "fingerprint"),
            hash=transport.fingerprint.hash,
            setup=transport.fingerprint.setup,
        )
        fingerprint.text = transport.fingerprint.value
    for candidate in transport.candidates:
        element.append(_build_candidate(candidate))
    return element


def _build_candidate(candidate: Candidate) -> ET.Element:
    element = ET.Element(
        _q(ICE_UDP, "candidate"),
        foundation=candidate.foundation,
        component=str(candidate.component),
        protocol=candidate.protocol,
        priority=str(candidate.priority),
        ip=candidate.ip,
        port=str(candidate.port),
        type=candidate.type,
        generation=str(candidate.generation),
        id=candidate.id,
        network="0",  # Dino sends it and Conversations may read it; the value is not used
    )
    if candidate.rel_addr is not None:
        element.set("rel-addr", candidate.rel_addr)
    if candidate.rel_port is not None:
        element.set("rel-port", str(candidate.rel_port))
    return element
