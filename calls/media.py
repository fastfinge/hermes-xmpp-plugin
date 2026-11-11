"""One call's media: an aiortc peer connection that answers a Jingle offer."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRelay
from aiortc.mediastreams import MediaStreamTrack
from aiortc.sdp import candidate_from_sdp as rtc_candidate

from .jingle.model import Candidate, Content, Jingle, UnsupportedError
from .jingle.sdp import (
    MAX_CANDIDATES,
    acceptable_candidate,
    answer_to_content,
    candidate_to_sdp,
    offer_to_sdp,
)

Transform = Callable[[MediaStreamTrack], MediaStreamTrack]


class _LateTrack(MediaStreamTrack):
    """The caller's audio for an outgoing call: aiortc creates the receiving track only once the
    peer's answer is applied, so readers wait for `resolve()` instead of getting None."""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._track: asyncio.Future[MediaStreamTrack] = asyncio.get_running_loop().create_future()

    def resolve(self, track: MediaStreamTrack) -> None:
        if not self._track.done():
            self._track.set_result(track)

    async def recv(self) -> Any:
        return await (await self._track).recv()


class CallMedia:
    """`make_outgoing` maps the caller's audio track to the track we send back (default: echo)."""

    def __init__(
        self, ice_servers: list[RTCIceServer], make_outgoing: Transform | None = None
    ) -> None:
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
        relay = MediaRelay()
        self._make_outgoing = make_outgoing or relay.subscribe
        self._ready = asyncio.Event()
        self._mid = ""
        self._transceiver: Any = None
        self._late: _LateTrack | None = None
        self.connected = asyncio.Event()
        self.on_lost: Callable[[], Awaitable[None]] | None = None  # ICE/DTLS failed or closed
        self._trickled = 0
        self.pc.on("connectionstatechange", self._on_state)

    async def _on_state(self) -> None:
        logging.info("media state %s", self.pc.connectionState)
        if self.pc.connectionState == "connected":
            self.connected.set()
        elif self.pc.connectionState in ("failed", "closed") and self.on_lost is not None:
            await self.on_lost()

    async def answer(self, offer: Jingle) -> Content:
        """Apply the offer and return the content for `session-accept`."""
        text = offer_to_sdp(offer)  # raises SecurityError / UnsupportedError on refusals
        offered = next(c for c in offer.contents if c.media == "audio")
        self._mid = offered.name
        await self.pc.setRemoteDescription(RTCSessionDescription(text, "offer"))
        if len(self.pc.getTransceivers()) != 1:  # one audio line, whatever the offer smuggles in
            raise UnsupportedError("offer must have exactly one audio section")
        for transceiver in self.pc.getTransceivers():
            self.pc.addTrack(self._make_outgoing(transceiver.receiver.track))
        await self.pc.setLocalDescription(await self.pc.createAnswer())
        self._ready.set()
        return answer_to_content(self.pc.localDescription.sdp, offered)

    async def offer(self) -> Content:
        """Outgoing call: build our offer. The peer's `session-accept` goes to `accept()`."""
        self._transceiver = self.pc.addTransceiver("audio", "sendrecv")
        self._late = _LateTrack()
        self._transceiver.sender.replaceTrack(self._make_outgoing(self._late))
        await self.pc.setLocalDescription(await self.pc.createOffer())
        content = answer_to_content(self.pc.localDescription.sdp, Content("initiator", "0"))
        self._mid = content.name
        return content

    async def accept(self, accepted: Jingle) -> None:
        """Apply the peer's `session-accept` to our offer."""
        text = offer_to_sdp(accepted)  # same checks: DTLS and Opus required
        self._mid = next(c.name for c in accepted.contents if c.media == "audio")
        await self.pc.setRemoteDescription(RTCSessionDescription(text, "answer"))
        if self._late is not None:
            self._late.resolve(self._transceiver.receiver.track)
        self._ready.set()

    async def add_candidate(self, candidate: Candidate) -> None:
        """Trickled remote candidate. Waits for the answer; ignores RTCP component 2 (rtcp-mux)."""
        await asyncio.wait_for(self._ready.wait(), 30)
        self._trickled += 1
        if candidate.component != 1 or not acceptable_candidate(candidate):
            return
        if self._trickled > MAX_CANDIDATES:
            return
        parsed = rtc_candidate(candidate_to_sdp(candidate))
        parsed.sdpMid, parsed.sdpMLineIndex = self._mid, 0
        await self.pc.addIceCandidate(parsed)

    async def packets(self) -> tuple[int, int]:
        """(received, sent) RTP packets so far; for logs and tests."""
        report = await self.pc.getStats()
        received = sum(
            getattr(s, "packetsReceived", 0) for s in report.values() if s.type == "inbound-rtp"
        )
        sent = sum(
            getattr(s, "packetsSent", 0) for s in report.values() if s.type == "outbound-rtp"
        )
        return received, sent

    async def close(self) -> None:
        await self.pc.close()
