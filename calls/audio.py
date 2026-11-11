"""Audio plumbing for calls: endpointing (VAD), playback track, WAV helpers. No network."""

import asyncio
import fractions
import io
import logging
import os
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass

import av
import numpy as np
from aiortc.mediastreams import MediaStreamTrack

STT_RATE = 16000
OUT_RATE = 48000
FRAME_MS = 20
STT_FRAME = STT_RATE * FRAME_MS // 1000
OUT_FRAME = OUT_RATE * FRAME_MS // 1000


@dataclass(frozen=True)
class SpeechHeld:
    """Caller has been voiced for `hold_ms`: long enough to be speech, not a click."""


@dataclass(frozen=True)
class Utterance:
    pcm: np.ndarray  # int16 mono at STT_RATE


def webrtc_vad(level: int = 2) -> Callable[[np.ndarray], bool]:
    """Frame classifier from the WebRTC VAD, after a high-pass pre-emphasis.

    Steady hum, fan rumble and hiss are rejected, speech and slow room noise changes are not
    confused for each other (measured on recorded noise and speech). Another voice in the
    background still counts as speech; no frame classifier can tell them apart.
    """
    import webrtcvad

    vad = webrtcvad.Vad(level)

    def is_speech(frame: np.ndarray) -> bool:
        x = frame.astype(np.float64)
        emphasized = np.concatenate(([x[0]], x[1:] - 0.97 * x[:-1]))
        return vad.is_speech(
            np.clip(emphasized, -32768, 32767).astype(np.int16).tobytes(), STT_RATE
        )

    return is_speech


class Endpointer:
    """Cuts 16 kHz mono int16 audio into utterances.

    An utterance opens on the first speech frame and closes after `silence_ms` without speech
    (or at `max_ms`). Shorter than `min_ms` of speech is a click and is dropped. `SpeechHeld`
    fires once speech has lasted `hold_ms`, which is what barge-in listens for.
    """

    def __init__(
        self,
        *,
        silence_ms: int = 800,
        min_ms: int = 300,
        max_ms: int = 30000,
        hold_ms: int = 300,
        preroll_ms: int = 200,
        is_speech: Callable[[np.ndarray], bool] | None = None,
    ) -> None:
        self.silence_ms, self.min_ms, self.max_ms, self.hold_ms = (
            silence_ms,
            min_ms,
            max_ms,
            hold_ms,
        )
        self._is_speech = is_speech or webrtc_vad()
        self._preroll = preroll_ms // FRAME_MS
        self._pending = np.zeros(0, np.int16)
        self._idle: list[np.ndarray] = []  # recent non-speech frames kept as preroll
        self._frames: list[np.ndarray] = []
        self._open = False
        self._voiced = self._silence = 0
        self._held = False

    def feed(self, pcm: np.ndarray) -> list[SpeechHeld | Utterance]:
        self._pending = np.concatenate([self._pending, pcm.astype(np.int16)])
        events: list[SpeechHeld | Utterance] = []
        while len(self._pending) >= STT_FRAME:
            frame, self._pending = self._pending[:STT_FRAME], self._pending[STT_FRAME:]
            events += self._frame(frame)
        return events

    def reset(self) -> None:
        """Forget audio heard so far (the caller was not being listened to)."""
        self._pending = np.zeros(0, np.int16)
        self._open, self._frames, self._idle = False, [], []

    def flush(self) -> list[Utterance]:
        """The caller says they stopped talking (client muted): end the open utterance now."""
        events: list[Utterance] = []
        if self._open and self._voiced >= self.min_ms:
            events.append(Utterance(np.concatenate(self._frames)))
        self._open, self._frames, self._idle = False, [], []
        return events

    def _frame(self, frame: np.ndarray) -> list[SpeechHeld | Utterance]:
        voiced = self._is_speech(frame)
        events: list[SpeechHeld | Utterance] = []
        if not self._open:
            if not voiced:
                if self._preroll:
                    self._idle = [*self._idle, frame][-self._preroll :]
                return events
            self._open, self._frames = True, [*self._idle, frame]
            self._idle, self._voiced, self._silence, self._held = [], FRAME_MS, 0, False
        else:
            self._frames.append(frame)
            if voiced:
                self._voiced, self._silence = self._voiced + FRAME_MS, 0
            else:
                self._silence += FRAME_MS
        if not self._held and self._voiced >= self.hold_ms:
            self._held = True
            events.append(SpeechHeld())
        if self._silence >= self.silence_ms or len(self._frames) * FRAME_MS >= self.max_ms:
            if self._voiced >= self.min_ms:
                events.append(Utterance(np.concatenate(self._frames)))
            self._open, self._frames = False, []
        return events


class Downsampler:
    """Decoded call frames (any rate/layout) to 16 kHz mono int16 for the endpointer and STT."""

    def __init__(self) -> None:
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=STT_RATE)

    def feed(self, frame: av.AudioFrame) -> np.ndarray:
        chunks = [f.to_ndarray().reshape(-1) for f in self._resampler.resample(frame)]
        return np.concatenate(chunks) if chunks else np.zeros(0, np.int16)


def ack_tone() -> np.ndarray:
    """A short soft beep (OUT_RATE, int16) played when an utterance is cut: "I heard you"."""
    t = np.arange(int(OUT_RATE * 0.08)) / OUT_RATE
    return (np.sin(2 * np.pi * 880 * t) * np.hanning(len(t)) * 3500).astype(np.int16)


def warn_tone() -> np.ndarray:
    """Two falling beeps: "this call ends in a few seconds unless you speak"."""
    parts: list[np.ndarray] = []
    for hz in (880, 660):
        t = np.arange(int(OUT_RATE * 0.12)) / OUT_RATE
        parts.append((np.sin(2 * np.pi * hz * t) * np.hanning(len(t)) * 4500).astype(np.int16))
        parts.append(np.zeros(int(OUT_RATE * 0.06), np.int16))
    return np.concatenate(parts)


def wav_bytes(pcm: np.ndarray, rate: int = STT_RATE) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.astype(np.int16).tobytes())
    return out.getvalue()


def decode_to_out_rate(path: str) -> np.ndarray:
    """Any audio file to 48 kHz mono int16 (what PlaybackTrack plays)."""
    resampler = av.AudioResampler(format="s16", layout="mono", rate=OUT_RATE)
    chunks: list[np.ndarray] = []
    with av.open(path) as container:
        for frame in container.decode(audio=0):
            chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
    chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]
    return np.concatenate(chunks) if chunks else np.zeros(0, np.int16)


MAX_TONE_S = 5  # a custom sound longer than this is cut


def load_tone(spec: bool | str, builtin: Callable[[], np.ndarray]) -> np.ndarray | None:
    """True: the built-in beep. False or "": no sound. A path: that audio file (anything PyAV
    reads), cut at MAX_TONE_S. A file that cannot be read falls back to the built-in beep."""
    if spec is True:
        return builtin()
    if not spec:
        return None
    try:
        clip = decode_to_out_rate(os.path.expanduser(spec))[: OUT_RATE * MAX_TONE_S]
    except Exception:
        logging.warning("calls: cannot read the sound %s; using the built-in beep", spec)
        return builtin()
    return clip if clip.size else builtin()


class PlaybackTrack(MediaStreamTrack):
    """What the caller hears: queued PCM, silence when empty, paced at real time."""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._buf = np.zeros(0, np.int16)
        self._start: float | None = None
        self._n = 0
        self.played = 0  # samples of real audio sent so far
        self._drained = asyncio.Event()

    def play(self, pcm: np.ndarray) -> None:
        self._buf = np.concatenate([self._buf, pcm])

    def flush(self) -> None:
        self._buf = np.zeros(0, np.int16)
        self._drained.set()

    async def wait_room(self) -> None:
        """Keep synthesis at most five seconds ahead, plus the next atomic sentence."""
        while len(self._buf) >= 5 * OUT_RATE:
            self._drained.clear()
            await asyncio.wait_for(self._drained.wait(), 60.0)

    @property
    def busy(self) -> bool:
        return len(self._buf) > 0

    async def wait_idle(self, limit: float = 60.0) -> None:
        """Wait until everything queued has been sent. recv() drains the buffer, so a call whose
        media died never drains: give up after `limit` seconds instead of waiting forever."""
        deadline = time.monotonic() + limit
        while self.busy and time.monotonic() < deadline:  # noqa: ASYNC110  no event to wait on
            await asyncio.sleep(FRAME_MS / 1000)

    async def recv(self) -> av.AudioFrame:
        if self._start is None:
            self._start = time.monotonic()
        await asyncio.sleep(max(0.0, self._start + self._n * FRAME_MS / 1000 - time.monotonic()))
        chunk, self._buf = self._buf[:OUT_FRAME], self._buf[OUT_FRAME:]
        if not self.busy:
            self.flush()
        self._drained.set()
        self.played += len(chunk)
        frame = av.AudioFrame.from_ndarray(
            np.pad(chunk, (0, OUT_FRAME - len(chunk))).reshape(1, -1), format="s16", layout="mono"
        )
        frame.sample_rate, frame.pts = OUT_RATE, self._n * OUT_FRAME
        frame.time_base = fractions.Fraction(1, OUT_RATE)
        self._n += 1
        return frame
