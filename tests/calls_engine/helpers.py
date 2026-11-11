"""Test helpers: a tone-sending aiortc caller and its Jingle offer."""

import asyncio
import fractions
import time
from dataclasses import replace
from typing import Any
from xml.etree import ElementTree as ET

import av
import numpy as np
from aiortc import RTCConfiguration, RTCPeerConnection
from aiortc.mediastreams import MediaStreamTrack

from calls.audio import Endpointer
from calls.jingle import wire
from calls.jingle.model import Candidate, Content, Jingle
from calls.jingle.sdp import answer_to_content

TONE_HZ = 440


def energy_endpointer(**kwargs: Any) -> Endpointer:
    """Endpointer whose "speech" is anything loud: synthetic tones are not speech to WebRTC."""
    return Endpointer(
        is_speech=lambda frame: float(np.sqrt(np.mean(frame.astype(float) ** 2))) > 1000, **kwargs
    )


class Tone(MediaStreamTrack):
    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self.start = time.time()
        self.n = 0

    async def recv(self) -> av.AudioFrame:
        await asyncio.sleep(max(0, self.start + self.n * 0.02 - time.time()))
        t = (np.arange(960) + self.n * 960) / 48000
        pcm = (np.sin(2 * np.pi * TONE_HZ * t) * 12000).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate, frame.pts, frame.time_base = (
            48000,
            self.n * 960,
            fractions.Fraction(1, 48000),
        )
        self.n += 1
        return frame


def over_the_wire(jingle: Jingle) -> Jingle:
    return wire.parse(ET.fromstring(ET.tostring(wire.build(jingle))))


async def make_offer(sid: str = "s1") -> tuple[RTCPeerConnection, Jingle, tuple[Candidate, ...]]:
    """An aiortc caller and its Conversations-style offer (candidates withheld for trickling)."""
    caller = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    caller.addTrack(Tone())
    await caller.setLocalDescription(await caller.createOffer())
    content = answer_to_content(caller.localDescription.sdp, Content("initiator", "0"))
    assert content.transport is not None
    bare = replace(content, transport=replace(content.transport, candidates=()))
    offer = over_the_wire(Jingle("session-initiate", sid, contents=(bare,), group=("0",)))
    return caller, offer, content.transport.candidates
