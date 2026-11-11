"""Outgoing-call media: the offerer must hear the answerer (regression: its receiving track
only exists after the answer is applied, so the call listener once got None)."""

import pytest
import asyncio

from aiortc.mediastreams import MediaStreamTrack

from .helpers import Tone, over_the_wire
from calls.jingle.model import Jingle
from calls.media import CallMedia


@pytest.mark.asyncio
async def test_offerer_receives_audio_from_the_answerer() -> None:
    heard = {"frames": 0}

    def attach(incoming: MediaStreamTrack) -> MediaStreamTrack:
        async def count() -> None:
            while True:
                await incoming.recv()
                heard["frames"] += 1

        readers.append(asyncio.ensure_future(count()))
        return Tone()

    readers: list[asyncio.Future[None]] = []
    caller = CallMedia([], make_outgoing=attach)
    callee = CallMedia([], make_outgoing=lambda _incoming: Tone())
    content = await caller.offer()
    offer = over_the_wire(
        Jingle("session-initiate", "s", contents=(content,), group=(content.name,))
    )
    answer = over_the_wire(
        Jingle("session-accept", "s", contents=(await callee.answer(offer),), group=(content.name,))
    )
    await caller.accept(answer)
    for candidate in content.transport.candidates:  # type: ignore[union-attr]
        await callee.add_candidate(candidate)

    await asyncio.wait_for(caller.connected.wait(), 15)
    await asyncio.sleep(1.5)
    await caller.close()
    await callee.close()
    for reader in readers:
        reader.cancel()
    assert heard["frames"] > 30
