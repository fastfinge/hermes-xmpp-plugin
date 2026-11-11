"""The call loop with fake STT/TTS/agent and a scripted caller track."""

import asyncio
import fractions
import itertools
import time

import av
import numpy as np
import pytest
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack

from .helpers import energy_endpointer
from calls.audio import OUT_FRAME, OUT_RATE, ack_tone, warn_tone
from calls.conversation import FILLERS, Conversation, Turn


def frame(pcm: np.ndarray, n: int) -> av.AudioFrame:
    f = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
    f.sample_rate, f.pts, f.time_base = OUT_RATE, n * OUT_FRAME, fractions.Fraction(1, OUT_RATE)
    return f


class Caller(MediaStreamTrack):
    """Plays a script of ("talk" | "quiet", ms) then ends the stream."""

    kind = "audio"

    def __init__(self, script: list[tuple[str, int]], stall: bool = False) -> None:
        super().__init__()
        self.stall = stall  # after the script, send nothing (a muted client) instead of ending
        self.frames = []
        for kind, ms in script:
            for _ in range(ms // 20):
                n = len(self.frames)
                t = np.arange(OUT_FRAME) + n * OUT_FRAME
                amp = 6000 if kind == "talk" else 10
                self.frames.append(frame((np.sin(t / 20) * amp).astype(np.int16), n))
        self.i = 0

    async def recv(self) -> av.AudioFrame:
        await asyncio.sleep(0)  # let the turn task run between frames
        if self.i >= len(self.frames):
            if self.stall:
                await asyncio.Event().wait()
            raise MediaStreamError
        self.i += 1
        return self.frames[self.i - 1]


class FakeSpeech:
    def __init__(self, transcript: str = "hello there") -> None:
        self.transcript, self.spoken = transcript, []

    async def transcribe(self, pcm: np.ndarray) -> str:
        return self.transcript

    async def synthesize(self, text: str) -> np.ndarray:
        self.spoken.append(text)
        return np.ones(OUT_RATE, np.int16)  # one second per sentence


async def settle(convo: Conversation) -> None:
    """Caller finished; give the turn time to queue audio. It then waits for playback to drain,
    which only happens when something calls playback.recv(), so do not await it to the end."""
    await convo._listener  # type: ignore[misc]
    if convo._task:
        await asyncio.wait({convo._task}, timeout=0.3)


@pytest.mark.asyncio
async def test_turn_is_transcribed_answered_and_spoken() -> None:
    speech, replies = FakeSpeech(), []

    async def respond(text: str) -> str:
        replies.append(text)
        return "I am fine, thank you for asking. How are you doing today, my friend?"

    async def hangup() -> None: ...

    convo = Conversation(
        speech,
        respond,
        hangup,
        endpointer=energy_endpointer(),
        barge_in="stop",
    )
    convo.attach(Caller([("quiet", 200), ("talk", 1000), ("quiet", 1000)]))
    await settle(convo)
    assert replies == ["hello there"]
    assert len(speech.spoken) == 2
    assert convo.playback.busy


@pytest.mark.asyncio
async def test_empty_transcript_makes_no_turn() -> None:
    called = []

    async def respond(text: str) -> str:
        called.append(text)
        return "x"

    async def hangup() -> None: ...

    convo = Conversation(
        FakeSpeech(""),
        respond,
        hangup,
        endpointer=energy_endpointer(),
        barge_in="stop",
    )
    convo.attach(Caller([("talk", 1000), ("quiet", 1000)]))
    await settle(convo)
    assert called == []


@pytest.mark.asyncio
async def test_hangup_marker_is_not_spoken_and_ends_call() -> None:
    speech, ended = FakeSpeech(), []

    async def respond(text: str) -> str:
        return "Goodbye, talk to you later. [[hangup]]"

    async def hangup() -> None:
        ended.append(True)

    convo = Conversation(
        speech,
        respond,
        hangup,
        endpointer=energy_endpointer(),
        barge_in="stop",
        hangup_grace=0,
    )
    convo.attach(Caller([("talk", 1000), ("quiet", 1000)]))
    await settle(convo)
    convo.playback.flush()  # as if playback finished
    await asyncio.wait({convo._task}, timeout=1)  # type: ignore[arg-type]
    assert ended == [True]
    assert all("hangup" not in s for s in speech.spoken)


@pytest.mark.asyncio
async def test_barge_in_flushes_audio_and_notes_heard_fraction() -> None:
    speech, seen = FakeSpeech(), []

    async def respond(text: str) -> str:
        seen.append(text)
        return "This is a rather long answer that goes on. And here is a second long sentence."

    async def hangup() -> None: ...

    convo = Conversation(
        speech,
        respond,
        hangup,
        endpointer=energy_endpointer(),
        barge_in="stop",
    )
    convo.attach(Caller([("talk", 1000), ("quiet", 1000)]))
    await settle(convo)
    assert convo.playback.busy
    for _ in range(50):  # 1 s of 2 s played
        await convo.playback.recv()
    convo._barge_in()
    assert not convo.playback.busy
    assert "interrupted" in convo._note
    assert "50%" in convo._note


def test_heard_fraction_math() -> None:
    turn = Turn(["aaaa", "bbbb"], start=100)
    turn.samples = [1000, 1000]
    assert turn.heard_fraction(100) == 0
    assert turn.heard_fraction(600) == 0.25
    assert turn.heard_fraction(2100) == 1


@pytest.mark.asyncio
async def test_muted_client_still_ends_the_utterance() -> None:
    """Dino sends no RTP while muted; silence must come from the clock."""
    replies = []

    async def respond(text: str) -> str:
        replies.append(text)
        return "Okay then, I heard you just fine."

    async def hangup() -> None: ...

    convo = Conversation(
        FakeSpeech(),
        respond,
        hangup,
        endpointer=energy_endpointer(),
        barge_in="stop",
    )
    convo.attach(Caller([("talk", 1000)], stall=True))
    await asyncio.sleep(1.8)
    convo.stop()
    assert replies == ["hello there"]


@pytest.mark.asyncio
async def test_slow_agent_gets_a_filler() -> None:
    speech = FakeSpeech()

    async def respond(text: str) -> str:
        await asyncio.sleep(0.15)
        return "The answer is long enough to be one sentence."

    async def hangup() -> None: ...

    convo = Conversation(
        speech,
        respond,
        hangup,
        filler_after=0.05,
        endpointer=energy_endpointer(),
        barge_in="stop",
    )
    convo.attach(Caller([("talk", 1000), ("quiet", 1000)]))
    await settle(convo)
    assert speech.spoken[0] in FILLERS
    assert speech.spoken[1].startswith("The answer")


@pytest.mark.asyncio
async def test_pause_mid_thought_is_joined_not_forgotten() -> None:
    """Two utterances close together, no reply yet: the agent is asked once with both."""
    sizes: list[int] = []

    class Counting(FakeSpeech):
        async def transcribe(self, pcm: np.ndarray) -> str:
            sizes.append(len(pcm))
            await asyncio.sleep(
                0.2
            )  # the first transcription is still running when the second arrives
            return "both parts"

    replies = []

    async def respond(text: str) -> str:
        replies.append(text)
        return "Understood, thank you very much."

    async def hangup() -> None: ...

    convo = Conversation(
        Counting(),
        respond,
        hangup,
        endpointer=energy_endpointer(),
        barge_in="stop",
    )
    convo.attach(Caller([("talk", 600), ("quiet", 900), ("talk", 600), ("quiet", 1000)]))
    await settle(convo)
    assert replies == ["both parts"]
    assert sizes[-1] > sizes[0] * 1.8  # the last transcription heard both utterances


@pytest.mark.asyncio
async def test_utterance_end_plays_the_ack_tone() -> None:
    async def respond(text: str) -> str:
        await asyncio.sleep(5)
        return "x"

    async def hangup() -> None: ...

    convo = Conversation(
        FakeSpeech(),
        respond,
        hangup,
        endpointer=energy_endpointer(),
        barge_in="stop",
        filler_after=60,
        ack_clip=ack_tone(),
    )
    convo.attach(Caller([("talk", 800), ("quiet", 1000)]))
    await settle(convo)
    assert convo.playback.busy
    assert 3000 < len(convo.playback._buf) < 5000  # just the beep: the agent has not answered
    convo.stop()


@pytest.mark.asyncio
async def test_failed_speech_falls_back_to_text() -> None:
    posted: list[str] = []

    class BrokenTTS(FakeSpeech):
        async def synthesize(self, text: str) -> np.ndarray:
            raise RuntimeError("tts down")

    async def respond(text: str) -> str:
        return "This reply cannot be spoken, sadly."

    async def hangup() -> None: ...

    async def post(text: str) -> None:
        posted.append(text)

    convo = Conversation(
        BrokenTTS(),
        respond,
        hangup,
        endpointer=energy_endpointer(),
        barge_in="stop",
        fallback_text=post,
    )
    convo.attach(Caller([("talk", 800), ("quiet", 1000)]))
    await settle(convo)
    await asyncio.sleep(2.5)  # two retries with backoff before giving up
    assert posted == ["This reply cannot be spoken, sadly."]


@pytest.mark.asyncio
async def test_protected_speech_ignores_the_caller_until_finished() -> None:
    """A briefing is delivered whole: no barge-in, no turn, no beep, whatever the caller says."""
    replies: list[str] = []

    class Short(FakeSpeech):
        async def synthesize(self, text: str) -> np.ndarray:
            return np.ones(OUT_RATE // 5, np.int16)

    async def respond(text: str) -> str:
        replies.append(text)
        return "Should not happen."

    async def hangup() -> None: ...

    convo = Conversation(Short(), respond, hangup, endpointer=energy_endpointer(), barge_in="stop")
    convo.attach(Caller([("talk", 1000), ("quiet", 1000)]))
    briefing = asyncio.create_task(
        convo.say(
            "Today the top options were A and B. The note published today is about A.",
            protected=True,
        )
    )
    await settle(convo)  # the caller talked and fell silent while the briefing was still queued
    assert replies == []
    assert convo._protected
    assert convo.playback.busy  # not flushed by a barge-in
    while convo.playback.busy:  # play it out
        await convo.playback.recv()
    await briefing
    assert not convo._protected
    assert replies == []


@pytest.mark.asyncio
async def test_unprotected_say_can_be_interrupted() -> None:
    convo = Conversation(
        FakeSpeech(),
        lambda _t: asyncio.sleep(0, "x"),
        lambda: asyncio.sleep(0),
        endpointer=energy_endpointer(),
        barge_in="stop",
    )
    await convo.say("Hello there my friend, how are you doing today?")
    assert convo.playback.busy
    convo.attach(Caller([("talk", 800), ("quiet", 100)]))
    await convo._listener  # type: ignore[misc]
    assert not convo.playback.busy or convo._ack is not None


@pytest.mark.asyncio
async def test_synthesis_is_retried_when_the_queue_is_full() -> None:
    attempts: list[str] = []

    class Flaky(FakeSpeech):
        async def synthesize(self, text: str) -> np.ndarray:
            attempts.append(text)
            if len(attempts) == 1:
                raise RuntimeError("queue_full")
            return np.ones(OUT_RATE, np.int16)

    convo = Conversation(Flaky(), lambda _t: asyncio.sleep(0, "x"), lambda: asyncio.sleep(0))
    await convo.say("A single sentence that is long enough to stand.")
    assert len(attempts) == 2
    assert convo.playback.busy


@pytest.mark.asyncio
async def test_failed_sentence_posts_the_rest_as_text() -> None:
    """The 'brief me' bug: one sentence failed and the briefing silently stopped."""
    posted: list[str] = []

    class FailsOnThird(FakeSpeech):
        async def synthesize(self, text: str) -> np.ndarray:
            if "third" in text:
                raise RuntimeError("queue_full")
            return np.ones(4800, np.int16)

    async def post(text: str) -> None:
        posted.append(text)

    convo = Conversation(
        FailsOnThird(),
        lambda _t: asyncio.sleep(0, "x"),
        lambda: asyncio.sleep(0),
        fallback_text=post,
    )
    await convo.say(
        "The first sentence is spoken fine. The second sentence is spoken fine. "
        "The third sentence cannot be made. The fourth sentence follows after it."
    )
    assert len(convo.playback._buf) == 2 * 4800
    assert posted == ["The third sentence cannot be made. The fourth sentence follows after it."]


@pytest.mark.asyncio
async def test_prepare_synthesizes_one_sentence_at_a_time() -> None:
    running = peak = 0

    class Counting(FakeSpeech):
        async def synthesize(self, text: str) -> np.ndarray:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1
            return np.ones(4800, np.int16)

    convo = Conversation(Counting(), lambda _t: asyncio.sleep(0, "x"), lambda: asyncio.sleep(0))
    text = (
        "Sentence number one is here. Sentence number two is here. Sentence number three is here."
    )
    convo.prepare(text)
    await convo.say(text)
    assert peak == 1
    assert len(convo.playback._buf) == 3 * 4800


@pytest.mark.asyncio
async def test_queue_mode_does_not_cut_the_agent_off_and_answers_afterwards() -> None:
    """Talking while the agent speaks: its audio keeps playing, your words become the next turn."""
    said: list[str] = []
    transcripts = iter(["first question", "and a follow up"])

    class Scripted(FakeSpeech):
        async def transcribe(self, pcm: np.ndarray) -> str:
            return next(transcripts)

    async def respond(text: str) -> str:
        said.append(text)
        return "This is a long enough answer to be spoken aloud."

    convo = Conversation(
        Scripted(),
        respond,
        lambda: asyncio.sleep(0),
        endpointer=energy_endpointer(),
    )
    convo.attach(Caller([("talk", 800), ("quiet", 1000)]))
    await settle(convo)
    assert said == ["first question"]
    assert convo.playback.busy
    spoken_before = len(convo.playback._buf)
    # the caller speaks again while the answer is still queued for playback
    await convo._hear(np.ones(16000, np.int16) * 3000)
    assert convo._queued == ["and a follow up"]
    assert len(convo.playback._buf) == spoken_before  # nothing was flushed
    while convo.playback.busy:  # the answer finishes playing
        await convo.playback.recv()
    for _ in range(50):
        if len(said) == 2:
            break
        await asyncio.sleep(0.02)
    assert said == ["first question", "and a follow up"]
    convo.stop()


@pytest.mark.asyncio
async def test_queue_mode_steers_a_turn_that_is_still_thinking() -> None:
    steered: list[str] = []
    release = asyncio.Event()

    async def respond(text: str) -> str:
        await release.wait()
        return "Done with that, thank you."

    async def steer(text: str) -> bool:
        steered.append(text)
        return True

    convo = Conversation(
        FakeSpeech("start the report"),
        respond,
        lambda: asyncio.sleep(0),
        steer=steer,
    )
    await convo._hear(np.ones(16000, np.int16))  # starts the turn
    await asyncio.sleep(0.05)
    assert convo._thinking
    await convo._hear(np.ones(16000, np.int16))  # said while the agent works
    assert steered == ["start the report"]
    assert convo._queued == []
    release.set()
    convo.stop()


@pytest.mark.asyncio
async def test_planned_hangup_warns_and_waits_for_speech() -> None:

    async def respond(text: str) -> str:
        return "x"

    async def hangup() -> None:
        pass

    convo = Conversation(FakeSpeech(), respond, hangup, warn_clip=warn_tone())
    assert await convo.grace(0.1) is False  # nobody spoke: time to hang up
    assert len(convo.playback._buf) == len(warn_tone())  # the warning beeps were queued
    loop = asyncio.get_running_loop()
    loop.call_later(0.05, convo._spoke.set)
    loop.call_later(0.1, convo._utterances.put_nowait, True)  # and the transcript held words
    assert await convo.grace(0.5) is True  # spoke in time: the call goes on
    assert await convo.grace(0) is False  # grace 0 turns the warning off


@pytest.mark.asyncio
async def test_goodbye_waits_out_the_grace_time_before_hanging_up() -> None:
    ended: list[bool] = []

    async def respond(text: str) -> str:
        return "Goodbye, talk to you later. [[hangup]]"

    async def hangup() -> None:
        ended.append(True)

    convo = Conversation(
        FakeSpeech(),
        respond,
        hangup,
        endpointer=energy_endpointer(),
        barge_in="stop",
        hangup_grace=0.4,
    )
    convo.attach(Caller([("talk", 1000), ("quiet", 1000)]))
    await settle(convo)
    convo.playback.flush()
    await asyncio.sleep(0.15)
    assert ended == []  # inside the grace window
    await asyncio.sleep(0.6)
    assert ended == [True]


@pytest.mark.asyncio
async def test_calling_back_after_a_missed_call_tells_the_agent() -> None:
    seen: list[str] = []

    async def respond(text: str) -> str:
        seen.append(text)
        return "It was about the backup."

    async def hangup() -> None:
        pass

    convo = Conversation(FakeSpeech(), respond, hangup, endpointer=energy_endpointer())
    convo.remember_missed("Your backup finished.")
    convo.attach(Caller([("talk", 1000), ("quiet", 1000)]))
    await settle(convo)
    assert seen
    assert "did not answer" in seen[0]
    assert "Your backup finished." in seen[0]


@pytest.mark.asyncio
async def test_noise_during_the_hangup_grace_does_not_keep_the_call_open(monkeypatch) -> None:
    async def respond(text: str) -> str:
        return "x"

    async def hangup() -> None:
        pass

    import calls.conversation as module

    monkeypatch.setattr(module, "NOISE_GRACE", 0.1)
    convo = Conversation(FakeSpeech(), respond, hangup)
    loop = asyncio.get_running_loop()
    loop.call_later(0.05, convo._spoke.set)  # the voice detector fired ...
    loop.call_later(0.1, convo._utterances.put_nowait, False)  # ... but the transcript was empty
    assert await convo.grace(0.3) is False  # so the call still ends


@pytest.mark.asyncio
async def test_hanging_up_mid_request_still_gets_the_request_handled() -> None:
    """The caller says something, the agent starts working, they hang up: the work finishes
    and the answer arrives as text; nothing is spoken."""
    texts: list[str] = []
    seen: list[str] = []

    async def respond(text: str) -> str:
        seen.append(text)
        await asyncio.sleep(1.0)  # tools running
        return "Done. The title is changed."

    async def hangup() -> None:
        pass

    async def post(text: str) -> None:
        texts.append(text)

    convo = Conversation(
        FakeSpeech("change the title"),
        respond,
        hangup,
        endpointer=energy_endpointer(),
        fallback_text=post,
        filler_after=0.05,
    )
    convo.attach(Caller([("talk", 600), ("quiet", 1000)], stall=True))
    await asyncio.sleep(0.4)  # utterance finished, the agent is working
    assert seen
    convo.playback.flush()
    await convo.finish()  # they hang up here
    assert texts == ["Done. The title is changed."]
    assert not convo.playback.busy  # nothing was spoken after the hang-up


@pytest.mark.asyncio
async def test_hanging_up_while_still_talking_handles_what_was_said() -> None:
    texts: list[str] = []

    async def respond(text: str) -> str:
        return "Noted."

    async def hangup() -> None:
        pass

    async def post(text: str) -> None:
        texts.append(text)

    convo = Conversation(
        FakeSpeech("remind me tomorrow"),
        respond,
        hangup,
        endpointer=energy_endpointer(),
        fallback_text=post,
    )
    convo.attach(Caller([("talk", 800)], stall=True))  # still speaking when the line drops
    await asyncio.sleep(0.3)  # the utterance is still open
    await convo.finish()
    assert texts == ["Noted."]


@pytest.mark.asyncio
async def test_idle_for_counts_silence_but_not_work_or_playback() -> None:
    async def respond(text: str) -> str:
        return "x"

    async def hangup() -> None:
        pass

    convo = Conversation(FakeSpeech(), respond, hangup)
    convo.last_active -= 100
    assert convo.idle_for() > 99
    convo.playback.play(np.zeros(160, np.int16))
    assert convo.idle_for() == 0.0  # still playing
    convo.playback.flush()
    convo._task = asyncio.create_task(asyncio.sleep(0.2))
    assert convo.idle_for() == 0.0  # the agent is working
    await convo._task
    assert convo.idle_for() > 99


@pytest.mark.asyncio
async def test_what_was_said_stays_out_of_the_log_unless_asked(caplog) -> None:
    import logging

    async def respond(text: str) -> str:
        return "A private answer."

    async def hangup() -> None:
        pass

    caplog.set_level(logging.INFO)
    convo = Conversation(
        FakeSpeech("my secret words"),
        respond,
        hangup,
        endpointer=energy_endpointer(),
    )
    convo.attach(Caller([("talk", 600), ("quiet", 1000)]))
    await settle(convo)
    assert "secret" not in caplog.text
    assert "caller: 3 words" in caplog.text
    convo2 = Conversation(
        FakeSpeech("my secret words"),
        respond,
        hangup,
        endpointer=energy_endpointer(),
        log_text=True,
    )
    convo2.attach(Caller([("talk", 600), ("quiet", 1000)]))
    await settle(convo2)
    assert "caller: my secret words" in caplog.text


@pytest.mark.asyncio
async def test_waiting_for_dead_media_gives_up() -> None:
    from calls.audio import PlaybackTrack

    playback = PlaybackTrack()
    playback.play(np.zeros(16000, np.int16))  # nobody pulls recv(): the media is dead
    started = time.monotonic()
    await playback.wait_idle(limit=0.3)
    assert time.monotonic() - started < 1.0


def _filler_convo(speech, tmp_path, fillers=("One moment.",)):
    async def respond(text: str) -> str:
        await asyncio.sleep(0.15)
        return "The answer is long enough to be one sentence."

    async def hangup() -> None: ...

    return Conversation(
        speech,
        respond,
        hangup,
        filler_after=0.05,
        fillers=fillers,
        filler_cache=tmp_path / "cache",
        endpointer=energy_endpointer(),
        barge_in="stop",
    )


@pytest.mark.asyncio
async def test_filler_audio_is_made_once_and_reused_across_calls(tmp_path) -> None:
    for _ in range(2):  # two calls, each with its own Conversation
        speech = FakeSpeech()
        convo = _filler_convo(speech, tmp_path)
        convo.attach(Caller([("talk", 1000), ("quiet", 1000)]))
        await settle(convo)
        assert speech.spoken.count("One moment.") == (1 if _ == 0 else 0)
    assert len(list((tmp_path / "cache").glob("phrase-*.npy"))) == 1


@pytest.mark.asyncio
async def test_empty_filler_says_nothing_and_caches_nothing(tmp_path) -> None:
    speech = FakeSpeech()
    convo = _filler_convo(speech, tmp_path, fillers=())
    convo.attach(Caller([("talk", 1000), ("quiet", 1000)]))
    await settle(convo)
    assert speech.spoken[0].startswith("The answer")
    assert not (tmp_path / "cache").exists()


@pytest.mark.asyncio
async def test_fillers_are_picked_at_random_and_never_repeat_back_to_back(tmp_path) -> None:
    speech = FakeSpeech()
    convo = _filler_convo(speech, tmp_path, fillers=("One moment.", "Hold on.", "Let me see."))
    picks = [await _pick(convo) for _ in range(30)]
    assert set(picks) == {"One moment.", "Hold on.", "Let me see."}
    assert all(a != b for a, b in itertools.pairwise(picks))
    # each phrase was synthesized once and saved
    assert sorted(speech.spoken) == ["Hold on.", "Let me see.", "One moment."]
    assert len(list((tmp_path / "cache").glob("phrase-*.npy"))) == 3


async def _pick(convo) -> str:
    await convo._filler_clip()
    return convo._last_filler


@pytest.mark.parametrize(
    "damage",
    [
        b"",
        b"not an array",
        b"PK\x03\x04 not an npy",
        b"\x93NUMPY\x01\x00v\x00{'descr': '<i2', 'fortran_order': False, 'shape': (2**60,), }"
        + b" " * 20
        + b"\n",
    ],
    ids=["empty", "text", "zip", "absurd-shape"],
)
@pytest.mark.asyncio
async def test_any_damaged_filler_file_is_replaced(tmp_path, damage) -> None:
    speech = FakeSpeech()
    convo = _filler_convo(speech, tmp_path)
    await convo._filler_clip()
    (path,) = (tmp_path / "cache").glob("phrase-*.npy")
    path.write_bytes(damage)
    again = FakeSpeech()
    convo = _filler_convo(again, tmp_path)
    await convo._filler_clip()
    assert again.spoken == ["One moment."]
    assert Conversation._load_clip(path) is not None  # rewritten, no temp files left
    assert [p.name for p in (tmp_path / "cache").iterdir()] == [path.name]


@pytest.mark.asyncio
async def test_greeting_is_one_cached_clip_and_a_dead_tts_falls_back_to_text(tmp_path) -> None:
    text = "Hi there. How can I help?"
    speech = FakeSpeech()
    convo = _filler_convo(speech, tmp_path)
    await convo.say(text, cached=True)
    assert speech.spoken == [text]  # one clip for the whole greeting, not one per sentence
    assert convo._turn is not None
    assert len(convo.playback._buf) == OUT_RATE
    assert convo._turn.parts == [text]
    again = FakeSpeech()
    await _filler_convo(again, tmp_path).say(text, cached=True)
    assert again.spoken == []  # second call: read from the cache file
    assert len(list((tmp_path / "cache").glob("phrase-*.npy"))) == 1

    class BrokenTTS(FakeSpeech):
        async def synthesize(self, text: str) -> np.ndarray:
            raise RuntimeError("tts down")

    posted: list[str] = []

    async def post(text: str) -> None:
        posted.append(text)

    convo = _filler_convo(BrokenTTS(), tmp_path / "other")
    convo.fallback_text = post
    await convo.say("Hello again.", cached=True)
    assert posted == ["Hello again."]


@pytest.mark.asyncio
async def test_a_ready_answer_does_not_wait_for_a_slow_filler(tmp_path) -> None:
    class SlowFiller(FakeSpeech):
        async def synthesize(self, text: str) -> np.ndarray:
            if text == "One moment.":
                await asyncio.sleep(2)
            return await super().synthesize(text)

    async def respond(text: str) -> str:
        await asyncio.sleep(0.1)
        return "ok"

    async def hangup() -> None: ...

    convo = Conversation(
        SlowFiller(),
        respond,
        hangup,
        filler_after=0.05,
        fillers=("One moment.",),
        endpointer=energy_endpointer(),
    )
    started = time.monotonic()
    assert await convo._answer("hi") == "ok"
    assert time.monotonic() - started < 1
