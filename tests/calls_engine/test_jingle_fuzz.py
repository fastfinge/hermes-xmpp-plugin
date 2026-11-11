"""Hostile Jingle input must raise JingleError (or parse), never anything else or hang."""

import contextlib
import random
from pathlib import Path
from xml.etree import ElementTree as ET

from calls.jingle import sdp, wire
from calls.jingle.model import JINGLE, JingleError

FIXTURES = Path(__file__).parent / "captures"
SAMPLES = ["conversations-session-initiate.xml", "dino-session-initiate.xml"]


def _mutate(root: ET.Element, rng: random.Random) -> None:
    nodes = list(root.iter())
    for _ in range(rng.randint(1, 4)):
        node = rng.choice(nodes)
        action = rng.choice(["attr", "drop-attr", "text", "drop-child", "dup-child"])
        if action == "attr" and node.attrib:
            node.set(
                rng.choice(list(node.attrib)),
                rng.choice(["", "-1", "x" * 5000, "99999999999", "\x00", "1e9"]),
            )
        elif action == "drop-attr" and node.attrib:
            del node.attrib[rng.choice(list(node.attrib))]
        elif action == "text":
            node.text = rng.choice(["", "garbage", "9" * 400, "\x01"])
        elif action == "drop-child" and len(node):
            node.remove(rng.choice(list(node)))
        elif action == "dup-child" and len(node):
            node.append(rng.choice(list(node)))


def test_mutated_offers_never_crash_the_parser() -> None:
    rng = random.Random(1234)
    parsed = 0
    for name in SAMPLES:
        original = ET.fromstring((FIXTURES / name).read_text())
        for _ in range(400):
            root = ET.fromstring(ET.tostring(original))
            _mutate(root, rng)
            element = root.find(f"{{{JINGLE}}}jingle")
            if element is None:
                continue
            try:
                offer = wire.parse(element)
                sdp.offer_to_sdp(offer)
                parsed += 1
            except JingleError:
                pass
    assert parsed > 0  # some mutations are harmless; the rest raised JingleError only


def test_hostile_candidate_lines_raise_jingle_error_only() -> None:
    for line in [
        "",
        "candidate:",
        "a=candidate:1 1 udp x y z",
        "candidate:1 1 udp 1 1.2.3.4 99999999 typ host",
        "x" * 10000,
        "candidate:1 1 tcp 1 ::1 1 typ \x00",
    ]:
        with contextlib.suppress(JingleError):
            sdp.candidate_from_sdp(line)
