"""Whole answer path without XMPP: an aiortc caller sends a tone through our Jingle/SDP bridge
and CallMedia echoes it back. Conversations-style: the caller's candidates are trickled."""

import pytest
import asyncio
from dataclasses import replace
from typing import cast

import av
import numpy as np
from aiortc import RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack

from .helpers import TONE_HZ, make_offer, over_the_wire
from calls.jingle.model import Jingle
from calls.jingle.sdp import offer_to_sdp
from calls.media import CallMedia


@pytest.mark.asyncio
async def test_tone_is_echoed_with_trickled_candidates() -> None:
    caller, offer, trickled = await make_offer()
    heard: list[np.ndarray] = []
    readers: list[asyncio.Future[None]] = []

    @caller.on("track")
    def on_track(track: MediaStreamTrack) -> None:
        async def read() -> None:
            while True:
                frame = await track.recv()
                heard.append(cast(av.AudioFrame, frame).to_ndarray().reshape(-1, 2)[:, 0])

        readers.append(asyncio.ensure_future(read()))

    media = CallMedia([])
    content = await media.answer(offer)
    accepted = over_the_wire(Jingle("session-accept", "s1", contents=(content,), group=("0",)))
    await caller.setRemoteDescription(RTCSessionDescription(offer_to_sdp(accepted), "answer"))
    bare = offer.contents[0]
    for candidate in trickled:
        update = replace(bare, transport=replace(bare.transport, candidates=(candidate,)))  # type: ignore[type-var]
        sent = over_the_wire(Jingle("transport-info", "s1", contents=(update,)))
        await media.add_candidate(sent.contents[0].transport.candidates[0])  # type: ignore[union-attr]

    await asyncio.wait_for(media.connected.wait(), 15)
    await asyncio.sleep(3)
    received, sent_packets = await media.packets()
    await media.close()
    await caller.close()

    assert received > 50
    assert sent_packets > 50
    x = np.concatenate(heard).astype(float)[-9600:]
    peak = np.argmax(np.abs(np.fft.rfft(x * np.hanning(len(x))))) * 48000 / len(x)
    assert abs(peak - TONE_HZ) < 20, peak
