"""Deterministic turn handoffs and shutdown with gated speech/agent providers."""

import asyncio
import time
import weakref
from contextlib import suppress

import numpy as np
import pytest
import pytest_asyncio

from .helpers import energy_endpointer
from .test_conversation import Caller, FakeSpeech
from calls.audio import OUT_FRAME, OUT_RATE, Endpointer, PlaybackTrack, Utterance
from calls.conversation import FILLERS, Conversation

PCM = np.ones(16000, np.int16)


class SilentSpeech(FakeSpeech):
    async def synthesize(self, text: str) -> np.ndarray:
        self.spoken.append(text)
        return np.zeros(0, np.int16)


async def spins() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


@pytest_asyncio.fixture
async def make_conversation():
    conversations = []

    def make(respond, **kwargs):
        convo = Conversation(
            kwargs.pop("speech", SilentSpeech("please handle these words")),
            respond,
            kwargs.pop("on_hangup", lambda: asyncio.sleep(0)),
            endpointer=energy_endpointer(),
            ack_tone_enabled=False,
            filler_after=kwargs.pop("filler_after", 999),
            **kwargs,
        )
        conversations.append(convo)
        return convo

    yield make
    for convo in conversations:
        tasks = {
            task
            for task in (
                *convo._background,
                convo._task,
                convo._listener,
                convo._prep,
                convo._grace,
            )
            if task is not None
        }
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_queued_words_override_a_goodbye(make_conversation) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    requests, ended = [], []

    async def respond(text: str) -> str:
        requests.append(text)
        entered.set()
        await release.wait()
        return "Goodbye. [[hangup]]" if len(requests) == 1 else ""

    async def hangup() -> None:
        ended.append(True)

    convo = make_conversation(respond, on_hangup=hangup, hangup_grace=0)
    convo._start_text("first request")
    await entered.wait()
    await convo._hear(PCM)
    await convo._hear(PCM)
    release.set()
    await spins()
    assert requests == ["first request", "please handle these words please handle these words"]
    assert ended == []
    assert convo._queued == []


@pytest.mark.asyncio
async def test_failed_reply_still_drains_queued_words(make_conversation) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def respond(text: str) -> str:
        requests.append(text)
        entered.set()
        await release.wait()
        if len(requests) == 1:
            raise RuntimeError("agent unavailable")
        return ""

    convo = make_conversation(respond)
    convo._start_text("first request")
    await entered.wait()
    await convo._hear(PCM)
    release.set()
    await spins()
    assert requests == ["first request", "please handle these words"]
    assert convo._queued == []


@pytest.mark.asyncio
async def test_rejected_steer_after_turn_finishes_starts_a_new_turn(make_conversation) -> None:
    entered, answer = asyncio.Event(), asyncio.Event()
    steering, reject = asyncio.Event(), asyncio.Event()
    requests = []

    async def respond(text: str) -> str:
        requests.append(text)
        entered.set()
        await answer.wait()
        return ""

    async def steer(text: str) -> bool:
        steering.set()
        await reject.wait()
        return False

    convo = make_conversation(respond, steer=steer)
    convo._start_text("first request")
    await entered.wait()
    hearing = asyncio.create_task(convo._hear(PCM))
    try:
        await steering.wait()
        answer.set()
        await convo._task
        reject.set()
        await hearing
        await spins()
        assert requests == ["first request", "please handle these words"]
        assert convo._queued == []
    finally:
        hearing.cancel()
        await asyncio.gather(hearing, return_exceptions=True)


@pytest.mark.parametrize("hangup", [False, True])
@pytest.mark.parametrize("delayed_sentence", [1, 2])
@pytest.mark.asyncio
async def test_finish_during_synthesis_posts_the_unspoken_reply(
    make_conversation, hangup: bool, delayed_sentence: int
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    posted, ended = [], []
    reply = "The requested action is complete. Here are the details you wanted."

    class GatedSpeech(SilentSpeech):
        async def synthesize(self, text: str) -> np.ndarray:
            self.spoken.append(text)
            if len(self.spoken) == delayed_sentence:
                entered.set()
                await release.wait()
            return np.ones(OUT_RATE, np.int16)

    async def post(text: str) -> None:
        posted.append(text)

    async def on_hangup() -> None:
        ended.append(True)

    convo = make_conversation(
        lambda text: asyncio.sleep(0, reply + (" [[hangup]]" if hangup else "")),
        speech=GatedSpeech(),
        fallback_text=post,
        on_hangup=on_hangup,
    )
    convo._start_text("first request")
    await entered.wait()
    finishing = asyncio.create_task(convo.finish(0.2))
    await spins()
    assert convo.closed
    release.set()
    await finishing
    assert not convo.playback.busy
    assert posted == [reply]
    assert ended == []


@pytest.mark.asyncio
async def test_filler_failure_preserves_the_successful_answer(make_conversation) -> None:
    filler = asyncio.Event()
    answer = "The requested action was completed."

    class BrokenFiller(SilentSpeech):
        async def synthesize(self, text: str) -> np.ndarray:
            if text in FILLERS:
                filler.set()
                raise RuntimeError("no filler audio")
            return await super().synthesize(text)

    async def respond(text: str) -> str:
        await filler.wait()
        return answer

    speech = BrokenFiller()
    convo = make_conversation(respond, speech=speech, filler_after=0)
    convo._start_text("first request")
    await convo._task
    assert convo.turns == 1
    assert speech.spoken == [answer]


@pytest.mark.parametrize("barge_in", ["queue", "stop"])
@pytest.mark.asyncio
async def test_stop_cancels_pending_transcripts_and_blocks_late_turns(
    make_conversation, barge_in: str
) -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()
    requests = []

    class GatedSpeech(SilentSpeech):
        async def transcribe(self, pcm: np.ndarray) -> str:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
            return "late words after cancellation"

    async def respond(text: str) -> str:
        requests.append(text)
        return ""

    convo = make_conversation(respond, speech=GatedSpeech(), barge_in=barge_in)
    convo._on_utterance(PCM)
    await entered.wait()
    convo.playback.play(PCM)
    convo.stop()
    convo._on_utterance(PCM)
    await spins()
    assert cancelled.is_set()
    assert requests == []
    assert not convo._background
    assert not convo.playback.busy
    assert convo.closed


@pytest.mark.asyncio
async def test_finish_deadline_cancels_and_awaits_owned_work(make_conversation) -> None:
    transcribing, thinking, preparing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    agent_cleaned = asyncio.Event()

    class StalledSpeech(SilentSpeech):
        async def transcribe(self, pcm: np.ndarray) -> str:
            transcribing.set()
            await asyncio.Event().wait()
            return ""

        async def synthesize(self, text: str) -> np.ndarray:
            preparing.set()
            await asyncio.Event().wait()
            return np.zeros(0, np.int16)

    async def respond(text: str) -> str:
        thinking.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            agent_cleaned.set()
        return ""

    convo = make_conversation(respond, speech=StalledSpeech())
    convo._start_text("first request")
    await thinking.wait()
    for _ in range(10):
        convo._on_utterance(PCM)
    convo.prepare("Prepare this sentence while the call rings.")
    await transcribing.wait()
    await preparing.wait()
    owned = {*convo._background, convo._task, convo._prep}
    convo.playback.play(PCM)
    await convo.finish(0.01)
    assert all(task.done() for task in owned)
    assert agent_cleaned.is_set()
    assert not convo._background
    assert not convo.playback.busy


@pytest.mark.asyncio
async def test_concurrent_grace_callers_share_the_same_words(
    make_conversation, monkeypatch
) -> None:
    import calls.conversation as module

    monkeypatch.setattr(module, "WORDS_WAIT", 0.01)
    monkeypatch.setattr(module, "NOISE_GRACE", 0.01)
    convo = make_conversation(lambda text: asyncio.sleep(0, ""))
    callers = [asyncio.create_task(convo.grace(0.05)) for _ in range(2)]
    try:
        await spins()
        convo._spoke.set()
        await convo._hear(PCM)
        assert await asyncio.gather(*callers) == [True, True]
    finally:
        for caller in callers:
            caller.cancel()
        await asyncio.gather(*callers, return_exceptions=True)


@pytest.mark.parametrize("preroll_ms", [0, 10])
def test_disabled_preroll_does_not_keep_silence(preroll_ms: int) -> None:
    endpoint = Endpointer(preroll_ms=preroll_ms, is_speech=lambda frame: bool(frame.any()))
    for _ in range(100):
        endpoint.feed(np.zeros(1600, np.int16))
    assert endpoint._idle == []
    endpoint.feed(PCM)
    [utterance] = endpoint.flush()
    assert isinstance(utterance, Utterance)
    np.testing.assert_array_equal(utterance.pcm, PCM)


@pytest.mark.asyncio
async def test_long_reply_is_backpressured_without_gaps_or_truncation(make_conversation) -> None:
    parts = [f"This is sentence number {index} in the long reply." for index in range(80)]

    class NumberedSpeech(SilentSpeech):
        async def synthesize(self, text: str) -> np.ndarray:
            self.spoken.append(text)
            return np.full(OUT_RATE, len(self.spoken), np.int16)

    speech = NumberedSpeech()
    convo = make_conversation(lambda text: asyncio.sleep(0, ""), speech=speech)
    speaking = asyncio.create_task(convo.say(" ".join(parts)))
    try:
        await spins()
        assert 1 <= len(speech.spoken) <= 6
        assert not speaking.done()
        convo.playback._start = time.monotonic() - 1000  # advance RTP without real-time sleeps
        for number in range(1, len(parts) + 1):
            for _ in range(50):
                frame = await convo.playback.recv()
                assert np.all(frame.to_ndarray() == number)
        await speaking
        assert speech.spoken == parts
        assert not convo.playback.busy
    finally:
        speaking.cancel()
        await asyncio.gather(speaking, return_exceptions=True)


@pytest.mark.parametrize("drain", [False, True])
@pytest.mark.asyncio
async def test_empty_playback_releases_its_audio(drain: bool) -> None:
    playback = PlaybackTrack()
    playback.play(np.ones(OUT_FRAME, np.int16))
    audio = weakref.ref(playback._buf)
    if drain:
        await playback.recv()
    else:
        playback.flush()
    assert not playback.busy
    assert audio() is None


@pytest.mark.asyncio
async def test_preparation_keeps_only_two_sentences_ahead(make_conversation) -> None:
    parts = [f"This is prepared sentence number {index}." for index in range(8)]

    class ShortSpeech(SilentSpeech):
        async def synthesize(self, text: str) -> np.ndarray:
            self.spoken.append(text)
            return np.ones(4800, np.int16)

    speech = ShortSpeech()
    convo = make_conversation(lambda text: asyncio.sleep(0, ""), speech=speech)
    convo.prepare(" ".join(parts))
    await spins()
    assert speech.spoken == parts[:2]
    await convo.say(" ".join(parts))
    assert speech.spoken == parts
    assert len(convo.playback._buf) == len(parts) * 4800


@pytest.mark.parametrize("kind", ["filler", "apology"])
@pytest.mark.asyncio
async def test_finish_blocks_late_auxiliary_audio(
    make_conversation, monkeypatch, kind: str
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    posted = []
    answer = "The requested action was completed."

    class GatedSpeech(SilentSpeech):
        async def synthesize(self, text: str) -> np.ndarray:
            entered.set()
            await release.wait()
            return np.ones(OUT_RATE, np.int16)

    async def respond(text: str) -> str:
        if kind == "apology":
            raise RuntimeError("agent unavailable")
        await entered.wait()
        return answer

    async def post(text: str) -> None:
        posted.append(text)

    convo = make_conversation(respond, speech=GatedSpeech(), filler_after=0, fallback_text=post)
    played = []
    play = convo.playback.play

    def record_play(audio: np.ndarray) -> None:
        played.append(convo.closed)
        play(audio)

    monkeypatch.setattr(convo.playback, "play", record_play)
    convo._start_text("first request")
    await entered.wait()
    finishing = asyncio.create_task(convo.finish(0.2))
    await spins()
    release.set()
    await finishing
    assert not convo.playback.busy
    assert posted == ([answer] if kind == "filler" else [])
    assert played == []


@pytest.mark.asyncio
async def test_finish_releases_prepared_audio(make_conversation) -> None:
    convo = make_conversation(lambda text: asyncio.sleep(0, ""), speech=FakeSpeech())
    text = "This sentence was prepared before the caller hung up."
    convo.prepare(text)
    await convo._prep
    audio = weakref.ref(convo._cache[text].result())
    await convo.finish(0)
    assert convo._cache == {}
    assert audio() is None


@pytest.mark.asyncio
async def test_finish_during_prepared_synthesis_keeps_text_fallback(make_conversation) -> None:
    entered = asyncio.Event()
    reply = "The requested action was completed."
    posted = []

    class StalledSpeech(SilentSpeech):
        async def synthesize(self, text: str) -> np.ndarray:
            entered.set()
            await asyncio.Event().wait()
            return np.zeros(0, np.int16)

    async def post(text: str) -> None:
        posted.append(text)

    convo = make_conversation(
        lambda text: asyncio.sleep(0, reply), speech=StalledSpeech(), fallback_text=post
    )
    convo.prepare(reply)
    await entered.wait()
    convo._start_text("first request")
    await spins()
    await convo.finish(0.1)
    assert posted == [reply]
    assert not convo.playback.busy


@pytest.mark.asyncio
async def test_stop_during_rejected_steer_does_not_requeue_words(make_conversation) -> None:
    thinking, steering = asyncio.Event(), asyncio.Event()
    requests = []

    async def respond(text: str) -> str:
        requests.append(text)
        thinking.set()
        await asyncio.Event().wait()
        return ""

    async def steer(text: str) -> bool:
        steering.set()
        with suppress(asyncio.CancelledError):
            await asyncio.Event().wait()
        return False

    convo = make_conversation(respond, steer=steer)
    convo._start_text("first request")
    await thinking.wait()
    convo._on_utterance(PCM)
    await steering.wait()
    convo.stop()
    await spins()
    assert requests == ["first request"]
    assert convo._queued == []
    assert not convo._background


@pytest.mark.parametrize("ignore_cancel", [False, True])
@pytest.mark.asyncio
async def test_stop_cancels_all_preparations(make_conversation, ignore_cancel: bool) -> None:
    entered = asyncio.Event()
    calls = []

    class StalledSpeech(SilentSpeech):
        async def synthesize(self, text: str) -> np.ndarray:
            calls.append(text)
            if len(calls) == 2:
                entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not ignore_cancel:
                    raise
            return np.zeros(0, np.int16)

    convo = make_conversation(lambda text: asyncio.sleep(0, ""), speech=StalledSpeech())
    tasks = []
    try:
        for text in ("The first preparation is pending.", "The second preparation is pending."):
            convo.prepare(text)
            tasks.append(convo._prep)
        await entered.wait()
        convo.stop()
        await spins()
        assert all(task.done() for task in tasks)
        assert all(task.cancelled() or task.exception() is None for task in tasks)
        convo.prepare("Do not synthesize after shutdown.")
        await spins()
        assert len(calls) == 2
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_finish_awaits_the_listeners_pending_receive(make_conversation) -> None:
    entered, cleaned = asyncio.Event(), asyncio.Event()

    class GatedCaller(Caller):
        async def recv(self):
            entered.set()
            try:
                return await super().recv()
            finally:
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                cleaned.set()

    convo = make_conversation(lambda text: asyncio.sleep(0, ""))
    convo.attach(GatedCaller([], stall=True))
    await entered.wait()
    await convo.finish(0)
    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_finish_also_awaits_a_replaced_turn(make_conversation) -> None:
    entered, cancelling = asyncio.Event(), asyncio.Event()
    calls = []

    async def respond(text: str) -> str:
        first = not calls
        calls.append(text)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            if first:
                cancelling.set()
                await asyncio.Event().wait()
        return ""

    convo = make_conversation(respond, barge_in="stop")
    convo._start_turn(PCM)
    await entered.wait()
    previous = convo._task
    try:
        convo._start_turn(PCM)
        await cancelling.wait()
        await convo.finish(0.01)
        assert previous.done()
        assert convo._task.done()
        assert not convo._background
    finally:
        previous.cancel()
        await asyncio.gather(previous, return_exceptions=True)
