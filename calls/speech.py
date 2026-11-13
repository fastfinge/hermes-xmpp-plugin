"""Reply text to speakable sentences. Pure."""

import re

HANGUP = "[[hangup]]"
_FENCE = re.compile(r"```.*?```", re.DOTALL)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_MARKUP = re.compile(r"[*_#>`]+|^\s*[-+]\s+|^\s*\d+\.\s+", re.MULTILINE)
_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_CLAUSE = re.compile(r"[,;:]\s")
FIRST_MAX = 70  # longer first chunks are split at a clause so speech starts sooner
MIN_SENTENCE = 25  # shorter pieces are merged forward: each TTS call costs ~1 s


# What Whisper-style models "hear" in room noise or silence.
_PHANTOMS = {"thank you", "thanks", "thank you for watching", "thanks for watching", "you"}
# A lone "bye" (or "by", which is what it sounds like) is never a goodbye, however long it took to
# say. "Bye bye", "Bye. Bye." and "goodbye" are not in this set and reach the agent.
_NEVER_A_GOODBYE = {"bye", "by"}
PHANTOM_MAX_S = 3.0


def is_phantom(text: str, seconds: float) -> bool:
    """A short utterance whose transcript is a stock phrase: noise, not the caller."""
    letters = re.sub(r"[^a-z ]", "", text.lower()).strip()
    if not any(c.isalnum() for c in text) or re.fullmatch(r"[\[(].*[\])]", text.strip()):
        return True  # "♪♪♪", "[music]", "(silence)": sound effects, not words
    if letters in _NEVER_A_GOODBYE:
        return True
    if letters in _PHANTOMS:
        return seconds < PHANTOM_MAX_S
    # "Thank you. Thank you. Thank you.": the same stock phrase over and over, at any length
    parts = [re.sub(r"[^a-z ]", "", p).strip() for p in re.split(r"[.!?]+", text.lower())]
    parts = [p for p in parts if p]
    # A mix such as "Thank you. Bye." stays noise (it was before); only "Bye. Bye." is speech.
    return (
        len(parts) >= 2
        and all(p in _PHANTOMS | _NEVER_A_GOODBYE for p in parts)
        and any(p in _PHANTOMS for p in parts)
    )


def clean(text: str) -> str:
    """Documented rule: code blocks become "code omitted", links keep their label, bare URLs
    become "a link", markdown marks and list bullets are dropped."""
    text = _FENCE.sub(" code omitted. ", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("a link", text)
    return _MARKUP.sub("", text).strip()


def split_hangup(text: str) -> tuple[str, bool]:
    return text.replace(HANGUP, "").strip(), HANGUP in text


def sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SPLIT.split(clean(text)) if p.strip()]
    merged: list[str] = []
    carry = ""
    for part in parts:
        carry = f"{carry} {part}".strip()
        if len(carry) >= MIN_SENTENCE:
            merged.append(carry)
            carry = ""
    if carry and merged:
        merged[-1] = f"{merged[-1]} {carry}"
    elif carry:
        merged.append(carry)
    return _quick_start(merged)


def _quick_start(parts: list[str]) -> list[str]:
    """Split a long first chunk at its first comma: TTS time grows with text length."""
    if parts and len(parts[0]) > FIRST_MAX:
        cut = _CLAUSE.search(parts[0], MIN_SENTENCE)
        if cut:
            return [parts[0][: cut.end()].strip(), parts[0][cut.end() :].strip(), *parts[1:]]
    return parts
