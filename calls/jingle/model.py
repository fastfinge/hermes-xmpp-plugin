"""Plain data for one Jingle audio session. Frozen, hashable, comparable in tests."""

from dataclasses import dataclass, field

JINGLE = "urn:xmpp:jingle:1"
RTP = "urn:xmpp:jingle:apps:rtp:1"
ICE_UDP = "urn:xmpp:jingle:transports:ice-udp:1"
DTLS = "urn:xmpp:jingle:apps:dtls:0"
RTCP_FB = "urn:xmpp:jingle:apps:rtp:rtcp-fb:0"
RTP_INFO = "urn:xmpp:jingle:apps:rtp:info:1"
HDREXT = "urn:xmpp:jingle:apps:rtp:rtp-hdrext:0"
SSMA = "urn:xmpp:jingle:apps:rtp:ssma:0"
GROUPING = "urn:xmpp:jingle:apps:grouping:0"


class JingleError(Exception):
    """A call cannot proceed; `reason` is the Jingle termination reason to send."""

    reason = "general-error"


class SecurityError(JingleError):
    reason = "security-error"


class UnsupportedError(JingleError):
    reason = "unsupported-applications"


@dataclass(frozen=True)
class PayloadType:
    id: int
    name: str
    clockrate: int
    channels: int = 1
    parameters: tuple[tuple[str, str], ...] = ()
    rtcp_fb: tuple[tuple[str, str | None], ...] = ()


@dataclass(frozen=True)
class Candidate:
    foundation: str
    component: int
    protocol: str
    priority: int
    ip: str
    port: int
    type: str
    generation: int = 0
    id: str = ""
    rel_addr: str | None = None
    rel_port: int | None = None


@dataclass(frozen=True)
class Fingerprint:
    hash: str
    setup: str
    value: str


@dataclass(frozen=True)
class Transport:
    ufrag: str
    pwd: str
    fingerprint: Fingerprint | None = None
    candidates: tuple[Candidate, ...] = ()


@dataclass(frozen=True)
class Content:
    creator: str
    name: str
    senders: str = "both"
    media: str | None = None
    payloads: tuple[PayloadType, ...] = ()
    header_extensions: tuple[tuple[int, str], ...] = ()
    rtcp_mux: bool = False
    transport: Transport | None = None


@dataclass(frozen=True)
class Jingle:
    action: str
    sid: str
    initiator: str | None = None
    contents: tuple[Content, ...] = ()
    group: tuple[str, ...] = field(default=())
    reason: str | None = None
    info: str | None = None  # session-info payload: "mute", "unmute", "active", "ringing", ...
