"""The call loop: caller audio -> endpointing -> STT -> agent -> sentence TTS -> playback,
with barge-in. Providers are injected, so the loop is testable without audio models."""

import asyncio
import hashlib
import logging
import os
import random
import tempfile
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Protocol

import numpy as np
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack

from .audio import (
    STT_RATE,
    Downsampler,
    Endpointer,
    PlaybackTrack,
    SpeechHeld,
    Utterance,
    ack_tone,
    warn_tone,
)
from .speech import is_phantom, sentences, split_hangup

OOPS = "Sorry, something went wrong. Please say that again."
FILLERS = (
    "One moment.",
    "Let me check.",
    "Working on it.",
    "Just a second.",
    "Give me a moment.",
    "Bear with me.",
)
MAX_FILLER_BYTES = 20_000_000  # a cached phrase is a few hundred kB; refuse anything absurd
GAP_S = 0.1  # no RTP for this long: the clock, not packets, advances endpointing


class Speech(Protocol):
    async def transcribe(self, pcm: np.ndarray) -> str: ...
    async def synthesize(self, text: str) -> np.ndarray: ...


WORDS_WAIT = (
    10.0  # hang-up grace: how long to wait for the transcript of something that sounded like speech
)
NOISE_GRACE = 2.0  # hang-up grace: time left to speak after noise turned out to hold no words


class Turn:
    """What was said in the current reply and how much of it has been queued as audio."""

    def __init__(self, parts: list[str], start: int) -> None:
        self.parts, self.start = parts, start
        self.samples: list[int] = []  # audio length per synthesized part, in order

    def heard_fraction(self, played: int) -> float:
        remaining, heard = played - self.start, 0.0
        for text, length in zip(self.parts, self.samples, strict=False):
            heard += len(text) * min(1.0, max(0.0, remaining / max(length, 1)))
            remaining -= length
        return heard / max(sum(len(p) for p in self.parts), 1)


async def retry[T](make: Callable[[], Awaitable[T]], tries: int = 3) -> T:
    """Run `make()`; on failure wait and try again (the shared audio queue may say queue_full)."""
    for attempt in range(tries):
        try:
            return await make()
        except asyncio.CancelledError:
            raise
        except Exception:
            if attempt == tries - 1:
                raise
            await asyncio.sleep(0.5 * 3**attempt)
    raise AssertionError("unreachable")


class Conversation:
    def __init__(
        self,
        speech: Speech,
        respond: Callable[[str], Awaitable[str]],
        on_hangup: Callable[[], Awaitable[None]],
        filler_after: float = 3.0,
        fillers: tuple[str, ...] = FILLERS,
        filler_cache: Path | None = None,
        endpointer: Endpointer | None = None,
        ack_tone_enabled: bool = True,
        fallback_text: Callable[[str], Awaitable[None]] | None = None,
        barge_in: str = "queue",
        hangup_grace: float = 5.0,
        log_text: bool = False,
        steer: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        # "queue": the caller can talk while the agent talks; nothing is cut off and what they say
        # becomes the next turn (or a steer, see below). "stop": speech cuts the agent off.
        self.barge_in = barge_in
        # Given text said while the agent is still working; True if the agent took it in.
        self.steer = steer
        self._queued: list[str] = []
        self._thinking = False
        self.hangup_grace = hangup_grace  # seconds the caller gets to speak before we hang up
        self._spoke = asyncio.Event()  # set whenever the caller starts to speak
        self._grace: asyncio.Task[bool] | None = None
        self._utterances: asyncio.Queue[bool] = asyncio.Queue()  # did each utterance hold words?
        self._hear_lock = asyncio.Lock()
        self._background: set[asyncio.Task[None]] = set()
        self.speech, self.respond, self.on_hangup = speech, respond, on_hangup
        self.fallback_text = fallback_text  # gets the reply as text when speaking it fails
        self._ack = ack_tone() if ack_tone_enabled else None
        self.filler_after = filler_after  # seconds of waiting for the agent before the filler
        self.fillers = fillers  # one is picked at random each time; () turns it off
        self.filler_cache = filler_cache  # directory holding the filler audio between calls
        self._filler_audio: dict[str, np.ndarray] = {}  # phrase -> audio, for this call
        self._last_filler = ""
        self._cache: dict[
            str, asyncio.Future[np.ndarray]
        ] = {}  # sentence -> audio being made ahead
        self._cache_changed = asyncio.Event()
        self._protected = False  # speaking something the caller must not interrupt
        self._prep: asyncio.Task[None] | None = None
        self._endpointer = endpointer or Endpointer()
        self._turn_pcm: np.ndarray | None = None  # caller audio of a turn with no reply yet
        self.playback = PlaybackTrack()
        self._task: asyncio.Task[None] | None = None  # the running turn
        self._listener: asyncio.Task[None] | None = None
        self._turn: Turn | None = None
        self._note = ""
        self.turns = 0  # caller turns the agent has answered
        self.closed = False  # the call is over: nothing is spoken, replies go out as text
        self._stopped = False  # unlike finish(), stop() discards remaining work
        self.log_text = log_text  # put what was said into the log (off: only its length)
        self.last_active = time.monotonic()  # last speech either way, for the idle timeout

    def attach(self, incoming: MediaStreamTrack) -> MediaStreamTrack:
        """CallMedia hook: start listening to the caller, return the track we speak on."""
        self._listener = asyncio.create_task(self._listen(incoming))
        return self.playback

    def prepare(self, text: str) -> None:
        """Synthesize `text` in the background, one sentence at a time (the TTS queue refuses
        parallel jobs), so `say()` has no gap. Results are consumed by `_speak`."""
        if self.closed:
            return
        parts = [p for p in sentences(text) if p not in self._cache]
        loop = asyncio.get_running_loop()
        futures = {p: self._cache.setdefault(p, loop.create_future()) for p in parts}

        async def run() -> None:
            try:
                for part in tuple(futures):
                    if self.closed:
                        return
                    future = futures[part]
                    while sum(f.done() for f in self._cache.values()) >= 2:
                        self._cache_changed.clear()
                        await self._cache_changed.wait()
                    try:
                        audio = await self._synth(part)
                        if not future.done():
                            future.set_result(audio)
                    except Exception as error:
                        if not future.done():
                            future.set_exception(error)
                    futures.pop(part)
            finally:
                for future in futures.values():
                    future.cancel()

        self._prep = asyncio.create_task(run())
        self._background.add(self._prep)
        self._prep.add_done_callback(self._background.discard)

    async def _filler_clip(self) -> np.ndarray:
        """A random filler (never the same twice in a row), played from this call's memory or the
        cache directory, else synthesized once and saved. The cache file name carries the phrase,
        so changing the phrases makes new files."""
        choices = [p for p in self.fillers if p != self._last_filler] or list(self.fillers)
        phrase = self._last_filler = random.choice(choices)
        if phrase in self._filler_audio:
            return self._filler_audio[phrase]
        path = None
        if self.filler_cache is not None:
            digest = hashlib.sha256(phrase.encode()).hexdigest()[:12]
            path = self.filler_cache / f"filler-{digest}.npy"
            if (clip := await asyncio.to_thread(self._load_clip, path)) is not None:
                return self._filler_audio.setdefault(phrase, clip)
        clip = await self._synth(phrase)
        if path is not None:
            await asyncio.to_thread(self._save_clip, path, clip)
        return self._filler_audio.setdefault(phrase, clip)

    @staticmethod
    def _load_clip(path: Path) -> np.ndarray | None:
        try:
            if path.stat().st_size > MAX_FILLER_BYTES:
                return None
            with open(path, "rb") as handle:  # np.load would leave a .npz archive open
                clip = np.load(handle, allow_pickle=False)
        except Exception:  # missing, empty, truncated, wrong format: the caller makes it again
            return None
        ok = isinstance(clip, np.ndarray) and clip.ndim == 1 and clip.dtype == np.int16
        return clip if ok and clip.size else None

    @staticmethod
    def _save_clip(path: Path, clip: np.ndarray) -> None:
        tmp = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
            with os.fdopen(fd, "wb") as handle:
                np.save(handle, clip, allow_pickle=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError:
            logging.warning("calls: could not save the filler audio to %s", path)
            if tmp is not None:
                with suppress(OSError):
                    os.unlink(tmp)

    async def _synth(self, text: str) -> np.ndarray:
        return await retry(lambda: self.speech.synthesize(text))

    def remember(self, spoken: str, context: str = "") -> None:
        """Tell the agent, on its next turn, what it said before the caller spoke, plus any
        `context` the agent that placed the call wants to keep (never spoken aloud)."""
        self._note = f"[You placed this call and began by saying: {spoken}] "
        if context:
            self._note += f"[Background for this call, not for the person: {context}] "

    def remember_missed(self, text: str) -> None:
        """The caller is calling back after a call we placed that nobody answered."""
        self._note = (
            "[You tried to phone this person earlier and they did not answer, so you texted "
            f"them this instead: {text}] "
        )

    async def say(self, text: str, *, protected: bool = False) -> None:
        """Speak without a turn (greeting, outgoing-call opening). With `protected`, the caller
        cannot interrupt: their speech is ignored until `text` has been played in full."""
        self._protected = protected
        self.touch()
        try:
            await self._speak(Turn(sentences(text), self.playback.played))
            if protected:
                await self.playback.wait_idle()
        finally:
            if protected:
                self._protected = False
                self._endpointer.reset()  # whatever they said meanwhile was not for us

    async def grace(self, seconds: float) -> bool:
        """About to hang up: warn with two falling beeps and give the caller `seconds` to speak.
        True if they did (the call goes on), False if it is time to hang up."""
        if seconds <= 0 or self.closed:
            return False
        if self._grace is None or self._grace.done():
            self._grace = asyncio.create_task(self._wait_for_speech(seconds))
        return await asyncio.shield(self._grace)

    async def _wait_for_speech(self, seconds: float) -> bool:
        self._spoke.clear()
        while not self._utterances.empty():
            self._utterances.get_nowait()
        self.playback.play(warn_tone())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while True:
            try:
                await asyncio.wait_for(self._spoke.wait(), max(0.0, deadline - loop.time()))
            except TimeoutError:
                return False
            if self.barge_in != "queue":
                return True
            # Something sounded like speech. Only words keep the call open, not room noise.
            try:
                words = await asyncio.wait_for(self._utterances.get(), WORDS_WAIT)
            except TimeoutError:
                words = False
            if words:
                return True
            self._spoke.clear()
            deadline = max(deadline, loop.time() + NOISE_GRACE)

    def caller_muted(self) -> None:
        """Jingle `mute` from the caller is a "stopped talking" signal: do not wait for silence."""
        if self._protected:
            return
        for utterance in self._endpointer.flush():
            self._on_utterance(utterance.pcm)

    def touch(self) -> None:
        self.last_active = time.monotonic()

    def idle_for(self) -> float:
        """Seconds since anybody spoke; a reply that is still playing counts as activity."""
        working = self._task is not None and not self._task.done()
        return 0.0 if working or self.playback.busy else time.monotonic() - self.last_active

    def _say_in_log(self, who: str, text: str) -> None:
        if self.log_text:
            logging.info("%s: %s", who, text)
        else:
            logging.info("%s: %d words", who, len(text.split()))

    async def finish(self, max_seconds: float = 180.0) -> None:
        """The call is over (the caller hung up, or time ran out). Hanging up does not cancel
        what was asked: stop listening and speaking, but still transcribe what was being said,
        let a running agent turn finish its work, and send its reply (and any queued speech's
        reply) to the chat as text."""
        self.closed = True
        if self._listener:
            self._listener.cancel()
        if self._prep:
            self._prep.cancel()
        self.playback.flush()
        for utterance in self._endpointer.flush():  # what they were saying as they hung up
            self._on_utterance(utterance.pcm)
        deadline = asyncio.get_running_loop().time() + max_seconds
        try:
            while True:
                running = {task for task in self._background if not task.done()}
                if self._task and not self._task.done():
                    running.add(self._task)
                running.discard(asyncio.current_task())
                left = deadline - asyncio.get_running_loop().time()
                if not running or left <= 0:
                    break
                await asyncio.wait(running, timeout=left)
        finally:
            remaining = {
                task
                for task in (*self._background, self._listener, self._task, self._prep, self._grace)
                if task is not None and task is not asyncio.current_task()
            }
            self.stop()
            await asyncio.gather(*remaining, return_exceptions=True)
            self.playback.flush()

    def stop(self) -> None:
        self.closed = self._stopped = True
        self.playback.flush()
        self._queued.clear()
        self._turn = self._turn_pcm = None
        self._filler_audio = {}
        self._endpointer.reset()
        for future in self._cache.values():
            if future.done() and not future.cancelled():
                future.exception()
            else:
                future.cancel()
        self._cache.clear()
        self._cache_changed.set()
        for task in (*self._background, self._listener, self._task, self._prep, self._grace):
            if task and task is not asyncio.current_task():  # hang-up runs inside the turn task
                task.cancel()

    async def _listen(self, track: MediaStreamTrack) -> None:
        down, endpointer = Downsampler(), self._endpointer
        pending = asyncio.ensure_future(track.recv())
        try:
            while True:
                done, _ = await asyncio.wait({pending}, timeout=GAP_S)
                if done:
                    pcm = down.feed(pending.result())  # type: ignore[arg-type]
                    pending = asyncio.ensure_future(track.recv())
                else:
                    # Clients stop sending RTP when muted; feed silence so utterances still end.
                    pcm = np.zeros(int(STT_RATE * GAP_S), np.int16)
                if self._protected:
                    continue  # speaking a briefing: the caller's audio is read and dropped
                for event in endpointer.feed(pcm):
                    if isinstance(event, SpeechHeld):
                        self.touch()
                        self._spoke.set()
                        if self.barge_in == "stop":
                            self._barge_in()
                    elif isinstance(event, Utterance):
                        logging.info("utterance %.1fs", len(event.pcm) / STT_RATE)
                        # "I heard you finish". In queue mode never mix it into a spoken reply.
                        if self._ack is not None and not (
                            self.barge_in == "queue" and self.playback.busy
                        ):
                            self.playback.play(self._ack)
                        self._on_utterance(event.pcm)
        except MediaStreamError:
            return
        except Exception:
            logging.exception("call listener crashed")
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    def _barge_in(self) -> None:
        """Caller spoke over us: stop audio, drop pending synthesis, remember what was heard."""
        if not (self.playback.busy or (self._task and not self._task.done())):
            return
        if self._turn and self.playback.busy:
            percent = round(self._turn.heard_fraction(self.playback.played) * 10) * 10
            self._note = (
                f"[The caller interrupted; they heard about {percent}% of your last reply.] "
            )
        self.playback.flush()
        if self._task:
            self._task.cancel()

    def _on_utterance(self, pcm: np.ndarray) -> None:
        if self._stopped:
            return
        if self.barge_in == "stop":
            self._start_turn(pcm)
            return
        task = asyncio.create_task(self._hear(pcm))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _hear(self, pcm: np.ndarray) -> None:
        """Queue mode: transcribe at once (in order), then hand the text on."""
        async with self._hear_lock:
            try:
                text = await retry(lambda: self.speech.transcribe(pcm))
            except Exception:
                logging.exception("transcription failed")
                self._utterances.put_nowait(False)
                return
            if self._stopped:
                return
            if not text or is_phantom(text, len(pcm) / STT_RATE):
                logging.info("heard no words in a %.1fs utterance", len(pcm) / STT_RATE)
                self._utterances.put_nowait(False)
                return
            self._say_in_log("caller", text)
            self._utterances.put_nowait(True)
            if self._thinking and self.steer is not None and await self.steer(text):
                logging.info("steered the running turn")
                return
            if self._stopped:
                return
            if self._task is None or self._task.done():
                self._start_text(text)
            else:
                self._queued.append(text)

    def _start_text(self, text: str) -> None:
        self._task = asyncio.create_task(self._run_turn(None, text))

    def _start_turn(self, pcm: np.ndarray) -> None:
        """Run a turn for `pcm`. If the previous utterance got no reply yet (the caller only
        paused mid-thought), it is replaced by both utterances joined, not forgotten."""
        if self._turn_pcm is not None:
            pcm = np.concatenate([self._turn_pcm, np.zeros(STT_RATE // 3, np.int16), pcm])
        self._turn_pcm = pcm
        if self._task and not self._task.done():
            self._task.cancel()
            self._background.add(self._task)
            self._task.add_done_callback(self._background.discard)
        self._task = asyncio.create_task(self._run_turn(pcm, None))

    async def _run_turn(self, pcm: np.ndarray | None, said: str | None) -> None:
        try:
            t0 = time.monotonic()
            if said is None:
                assert pcm is not None
                text = await retry(lambda: self.speech.transcribe(pcm))
                if not text or is_phantom(text, len(pcm) / STT_RATE):
                    logging.info("heard no words in a %.1fs utterance", len(pcm) / STT_RATE)
                    self._turn_pcm = None
                    return
                self._say_in_log("caller", text)
            else:
                text = said
            if self._stopped:
                return
            t1 = time.monotonic()
            reply, hangup = split_hangup(await self._answer(self._note + text))
            self._note = ""
            self.turns += 1
            self._turn_pcm = None  # answered: what the caller says next is a new turn
            t2 = time.monotonic()
            self._say_in_log("agent", reply)
            self.touch()
            if self.closed:  # they hung up: the answer still reaches them, as text
                if reply and self.fallback_text is not None:
                    await self.fallback_text(reply)
                hangup = False
            else:
                await self._speak(Turn(sentences(reply), self.playback.played))
                logging.info(
                    "timing stt=%.1fs agent=%.1fs first-audio=%.1fs",
                    t1 - t0,
                    t2 - t1,
                    time.monotonic() - t2,
                )
                await self.playback.wait_idle()
            if hangup:
                logging.info("hang-up requested: %.0fs to speak", self.hangup_grace)
            if (
                hangup
                and not self.closed
                and not self._queued
                and not await self.grace(self.hangup_grace)
                and not self.closed
                and not self._queued
            ):
                logging.info("nobody spoke: hanging up")
                await self.on_hangup()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("turn failed")
            self._turn_pcm = None
            await self._apologize()
        finally:
            task = asyncio.current_task()
            if self._queued and self._task is task and task is not None and not task.cancelling():
                # said while we were busy: that is the next turn
                self._start_text(" ".join(self._queued))
                self._queued.clear()

    async def _answer(self, text: str) -> str:
        """Ask the agent; if it is slow, say a short filler so the line is not dead."""
        reply = asyncio.ensure_future(self.respond(text))
        filler: asyncio.Task[None] | None = None
        self._thinking = True
        try:
            done, _ = await asyncio.wait({reply}, timeout=self.filler_after)
            if not done and not self.closed and self.fillers:
                # its own task: an answer that is ready never waits for the filler
                filler = asyncio.create_task(self._say_filler(reply))
            return await reply
        finally:
            self._thinking = False
            reply.cancel()  # no-op when finished; stops the agent call on barge-in
            if filler is not None:
                filler.cancel()
            await asyncio.gather(reply, *([filler] if filler else []), return_exceptions=True)

    async def _say_filler(self, reply: asyncio.Future[str]) -> None:
        try:
            clip = await self._filler_clip()
            if not self.closed and not reply.done():
                self.playback.play(clip)
        except Exception:
            logging.exception("filler synthesis failed; still waiting for the answer")

    async def _speak(self, turn: Turn) -> None:
        """Queue every sentence of `turn` for playback. A sentence that cannot be synthesized
        (after retries) ends the speech and the rest is posted to chat as text."""
        self._turn = turn
        for _index, part in enumerate(turn.parts):
            try:
                if self.closed:
                    break
                await self.playback.wait_room()
                if self.closed:
                    break
                ahead = self._cache.pop(part, None)
                self._cache_changed.set()
                audio = await ahead if ahead is not None else await self._synth(part)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if self.closed and task is not None and not task.cancelling():
                    break  # preparation stopped at hang-up; the turn still owes its text
                raise
            except Exception:
                logging.exception("speech synthesis failed; sending the rest as text")
                break
            if self.closed:
                break
            turn.samples.append(len(audio))
            self.playback.play(audio)
        else:
            return
        if self.fallback_text is not None:
            await self.fallback_text(" ".join(turn.parts[0 if self.closed else _index :]))

    async def _apologize(self) -> None:
        if self.closed:
            return
        try:
            audio = await self._synth(OOPS)
            if not self.closed:
                self.playback.play(audio)
        except Exception:
            logging.exception("TTS also failed; staying silent")
