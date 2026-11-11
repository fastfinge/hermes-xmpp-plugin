import numpy as np

from .helpers import energy_endpointer
from calls.audio import (
    MAX_TONE_S,
    OUT_RATE,
    STT_FRAME,
    Endpointer,
    SpeechHeld,
    Utterance,
    ack_tone,
    load_tone,
    wav_bytes,
)

MS = STT_FRAME // 20  # samples per ms at 16 kHz


def tone(ms: int, amp: int = 5000) -> np.ndarray:
    return (np.sin(np.arange(ms * MS) / 7) * amp).astype(np.int16)


def quiet(ms: int) -> np.ndarray:
    return (np.random.default_rng(0).normal(0, 20, ms * MS)).astype(np.int16)


def run(*chunks: np.ndarray) -> list:
    endpointer = energy_endpointer()
    events = []
    for chunk in chunks:
        events += endpointer.feed(chunk)
    return events


def test_one_utterance_after_silence() -> None:
    events = run(quiet(400), tone(1000), quiet(900))
    assert [type(e) for e in events] == [SpeechHeld, Utterance]
    assert len(events[1].pcm) // MS == 2000  # 200 preroll + 1000 speech + 800 silence


def test_click_is_ignored() -> None:
    assert run(quiet(300), tone(60), quiet(1500)) == []


def test_two_sentences_make_two_utterances() -> None:
    events = run(tone(800), quiet(900), tone(800), quiet(900))
    assert [type(e) for e in events] == [SpeechHeld, Utterance, SpeechHeld, Utterance]


def test_max_length_cuts_monologue() -> None:
    endpointer = energy_endpointer(max_ms=2000)
    events = endpointer.feed(tone(5000))
    assert sum(isinstance(e, Utterance) for e in events) >= 2


def test_chunking_does_not_matter() -> None:
    signal = np.concatenate([quiet(400), tone(1000), quiet(900)])
    endpointer = energy_endpointer()
    events = []
    for i in range(0, len(signal), 97):  # odd chunk size
        events += endpointer.feed(signal[i : i + 97])
    assert [type(e) for e in events] == [SpeechHeld, Utterance]


def test_default_vad_ignores_steady_noise() -> None:
    """The old energy gate held the call open on any hum or hiss above its threshold."""
    rng = np.random.default_rng(1)
    t = np.arange(16000 * 6)
    hum = np.sin(2 * np.pi * 100 * t / 16000) * 1500 + rng.normal(0, 200, len(t))
    fan = np.convolve(rng.normal(0, 800, len(t)), np.ones(40) / 40, "same")
    for noise in (hum, fan, rng.normal(0, 500, len(t))):
        assert Endpointer().feed(noise.astype(np.int16)) == []


def test_ack_tone_is_short_and_quiet() -> None:
    tone = ack_tone()
    assert 3000 < len(tone) < 5000  # about 80 ms at 48 kHz
    assert np.abs(tone).max() < 8000


def test_tone_setting_builtin_off_file_cut_and_unreadable_file(tmp_path) -> None:
    builtin = ack_tone

    def length(spec: bool | str) -> int:
        clip = load_tone(spec, builtin)
        assert clip is not None
        return len(clip)

    assert length(True) == len(builtin())
    assert load_tone(False, builtin) is None
    assert load_tone("", builtin) is None
    short = tmp_path / "chime.wav"
    short.write_bytes(wav_bytes(np.ones(1600, np.int16), OUT_RATE))
    assert length(str(short)) == 1600  # the file, at the call's rate
    long = tmp_path / "long.wav"
    long.write_bytes(wav_bytes(np.ones(OUT_RATE * (MAX_TONE_S + 3), np.int16), OUT_RATE))
    assert length(str(long)) == OUT_RATE * MAX_TONE_S
    assert length(str(tmp_path / "missing.wav")) == len(builtin())
