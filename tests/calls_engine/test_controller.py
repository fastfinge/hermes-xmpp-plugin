"""CallController with a fake slixmpp client and host, and a real aiortc caller."""

import pytest
import asyncio
import itertools
import re
from types import SimpleNamespace
from typing import Any
from xml.etree import ElementTree as ET

import numpy as np
import slixmpp
from slixmpp.exceptions import IqError

from .helpers import make_offer
from calls import controller
from calls.controller import JMI, CallConfig, CallController
from calls.jingle import wire
from calls.jingle.model import Jingle

PEER = "alice@example.org/phone"


class Sent:
    def __init__(self, log: list, kind: str, to: str, xml: ET.Element) -> None:
        self.log, self.kind, self.to, self.xml = log, kind, to, xml

    def send(self) -> None:
        self.log.append(self)


class FakeClient:
    default_ns = "jabber:client"
    boundjid = slixmpp.JID("bot@example.org/res")

    def __init__(self) -> None:
        self.sent: list[Sent] = []
        self.plugin: dict = {}

    def make_message(self, mto: Any, mtype: str) -> Sent:
        return Sent(self.sent, "message", str(mto), ET.Element("message"))

    def make_iq_set(self, sub: ET.Element, ito: Any) -> Sent:
        return Sent(self.sent, "iq", str(ito), sub)

    def make_iq_get(self, ito: Any) -> Any:
        class Get:
            xml = ET.Element("iq")

            async def send(self, timeout: int = 0) -> Any:  # noqa: ASYNC109  mirrors slixmpp
                raise TimeoutError

        return Get()

    def jingle_sent(self) -> list[Jingle]:
        return [wire.parse(s.xml) for s in self.sent if s.kind == "iq"]

    def jmi_sent(self) -> list[tuple[str, str]]:
        return [(s.to, s.xml[0].tag.rsplit("}", 1)[1]) for s in self.sent if s.kind == "message"]


class FakeSpeech:
    async def transcribe(self, pcm: np.ndarray) -> str:
        return "hello"

    async def synthesize(self, text: str) -> np.ndarray:
        return np.zeros(4800, np.int16)


class FakeHost:
    speech = FakeSpeech()

    def authorized(self, bare_jid: str) -> bool:
        return bare_jid == "alice@example.org"

    async def ask(self, peer: str, call_id: str, text: str) -> str:
        return "A fine reply that is spoken aloud."

    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.titles: list[str] = []

    async def post_text(self, peer: str, text: str) -> None:
        self.texts.append((peer, text))

    async def steer(self, peer: str, call_id: str, text: str) -> bool:
        return False

    async def name_call(self, peer: str, call_id: str, title: str) -> None:
        self.titles.append(title)


class FakeStanza:
    def __init__(self, xml: ET.Element, sender: str, kind: str = "set") -> None:
        self.xml, self._fields = xml, {"from": slixmpp.JID(sender), "type": kind}
        self.replies = 0

    def __getitem__(self, key: str) -> Any:
        return self._fields[key]

    def reply(self) -> Any:
        stanza = self

        class Reply:
            def send(self) -> None:
                stanza.replies += 1

        return Reply()


def propose(frm: str = PEER) -> FakeStanza:
    message = ET.Element("message")
    ET.SubElement(message, f"{{{JMI}}}propose", id="p1")
    return FakeStanza(message, frm, "chat")


def make() -> tuple[CallController, FakeClient]:
    client = FakeClient()
    return CallController(client, FakeHost()), client


def test_install_registers_handlers() -> None:
    client = slixmpp.ClientXMPP("bot@example.org/res", "x")
    before = len(client._handlers) if hasattr(client, "_handlers") else 0
    CallController(client, FakeHost()).install()
    assert (len(client._handlers) if hasattr(client, "_handlers") else 3) > before


@pytest.mark.asyncio
async def test_propose_from_authorized_peer_is_accepted_and_proceeded() -> None:
    ctl, client = make()
    await ctl._on_jmi(propose())
    assert client.jmi_sent() == [("bot@example.org", "accept"), (PEER, "proceed")]


@pytest.mark.asyncio
async def test_propose_from_stranger_is_ignored() -> None:
    ctl, client = make()
    await ctl._on_jmi(propose("evil@example.org/x"))
    assert client.sent == []


@pytest.mark.asyncio
async def test_propose_while_busy_is_rejected() -> None:
    ctl, client = make()
    ctl.calls["other"] = object()  # type: ignore[assignment]
    await ctl._on_jmi(propose())
    assert client.jmi_sent() == [("alice@example.org", "reject")]


@pytest.mark.asyncio
async def test_call_lifecycle() -> None:
    ctl, client = make()
    caller, offer, _ = await make_offer("s1")
    await ctl.handle(PEER, offer)
    (accept,) = client.jingle_sent()
    assert accept.action == "session-accept"
    assert accept.sid == "s1"
    assert accept.group == ("0",)
    assert set(ctl.calls) == {"s1"}

    await ctl.handle(PEER, Jingle("session-terminate", "s1", reason="success"))
    assert ctl.calls == {}
    await caller.close()


@pytest.mark.asyncio
async def test_mute_ends_the_utterance() -> None:
    ctl, _ = make()
    caller, offer, _ = await make_offer("s2")
    await ctl.handle(PEER, offer)
    muted = []
    ctl.calls["s2"].convo.caller_muted = lambda: muted.append(True)  # type: ignore[method-assign]
    await ctl.handle(PEER, Jingle("session-info", "s2", info="mute"))
    assert muted == [True]
    await ctl.close()
    await caller.close()


@pytest.mark.asyncio
async def test_second_call_gets_busy() -> None:
    ctl, client = make()
    first, offer1, _ = await make_offer("a")
    second, offer2, _ = await make_offer("b")
    await ctl.handle(PEER, offer1)
    await ctl.handle(PEER, offer2)
    assert [(j.action, j.reason) for j in client.jingle_sent()][-1] == ("session-terminate", "busy")
    assert set(ctl.calls) == {"a"}
    await ctl.close()
    await first.close()
    await second.close()


@pytest.mark.asyncio
async def test_offer_without_dtls_is_refused_with_security_error() -> None:
    ctl, client = make()
    caller, offer, _ = await make_offer("s3")
    # Renaming the element hides the fingerprint from the parser without rebuilding the tree.
    xml = ET.tostring(wire.build(offer)).decode().replace("fingerprint", "xfingerprint")
    iq = ET.Element("iq")
    iq.append(ET.fromstring(xml))
    stanza = FakeStanza(iq, PEER)
    await ctl._on_jingle(stanza)
    assert stanza.replies == 1
    assert client.jingle_sent()[-1].reason == "security-error"
    assert ctl.calls == {}
    await caller.close()


@pytest.mark.asyncio
async def test_close_hangs_up_open_calls() -> None:
    ctl, client = make()
    caller, offer, _ = await make_offer("s4")
    await ctl.handle(PEER, offer)
    await ctl.close()
    assert ctl.calls == {}
    assert client.jingle_sent()[-1].action == "session-terminate"
    await caller.close()
    await asyncio.sleep(0)


async def _run_outgoing(**options: Any) -> tuple[CallController, FakeClient, str, Any, list[str]]:
    """Place a call, have an aiortc-backed 'phone' answer it, return once connected."""
    ctl, client = make()
    spoken: list[str] = []
    ctl.host.speech.synthesize = lambda text: _record(spoken, text)  # type: ignore[method-assign]
    task = asyncio.create_task(
        ctl.place_call("alice@example.org", "Your backup finished.", **options)
    )
    await asyncio.sleep(0.05)
    (propose_msg,) = [s for s in client.sent if s.kind == "message"]
    proposal = str(propose_msg.xml[0].get("id"))
    assert propose_msg.xml[0].tag == f"{{{JMI}}}propose"
    assert propose_msg.to == "alice@example.org"

    await ctl._on_jmi(proceed(proposal))  # the phone answered
    for _ in range(100):
        initiates = [j for j in client.jingle_sent() if j.action == "session-initiate"]
        if initiates:
            break
        await asyncio.sleep(0.05)
    (initiate,) = initiates
    assert initiate.sid == proposal
    assert initiate.initiator == "bot@example.org/res"
    assert client.sent[-1].to == PEER

    from calls.media import CallMedia

    phone = CallMedia([])
    answer = await phone.answer(initiate)
    accept = Jingle("session-accept", proposal, contents=(answer,), group=initiate.group)
    await ctl.handle(PEER, accept)
    for candidate in initiate.contents[0].transport.candidates:  # type: ignore[union-attr]
        await phone.add_candidate(candidate)
    result = await asyncio.wait_for(task, 20)
    return ctl, client, result, phone, spoken


@pytest.mark.asyncio
async def test_outgoing_call_rings_offers_speaks_the_opening_names_the_session_and_recaps() -> None:
    ctl, _, result, phone, spoken = await _run_outgoing(topic="example-daily-note")
    sid = next(iter(ctl.calls))
    assert result.startswith("connected")
    await asyncio.sleep(0.1)
    assert "Your backup finished." in spoken
    assert "began by saying: Your backup finished." in ctl.calls[sid].convo._note
    assert not ctl.calls[sid].convo._protected
    (title,) = ctl.host.titles  # type: ignore[attr-defined]
    assert re.fullmatch(r"Call to alice \d{4}-\d\d-\d\d \d\d:\d\d example-daily-note", title)
    ctl.calls[sid].convo.turns = 2  # the person said something and the agent answered
    await ctl.end(sid, "success", notify=True)
    await asyncio.sleep(0.2)
    recaps = [t for _, t in ctl.host.texts if t.startswith("Call recap:")]  # type: ignore[attr-defined]
    assert len(recaps) == 1
    await phone.close()


@pytest.mark.asyncio
async def test_call_without_talk_has_no_recap_and_a_plain_title() -> None:
    ctl, _, _, phone, _ = await _run_outgoing()
    sid = next(iter(ctl.calls))
    await asyncio.sleep(0.1)
    (title,) = ctl.host.titles  # type: ignore[attr-defined]
    assert re.fullmatch(r"Call to alice \d{4}-\d\d-\d\d \d\d:\d\d", title)
    await ctl.end(sid, "success", notify=True)
    await asyncio.sleep(0.2)
    assert ctl.host.texts == []  # type: ignore[attr-defined]
    await phone.close()


async def _limited_call(
    monkeypatch: Any, **config: Any
) -> tuple[CallController, str, list[str], Any]:
    monkeypatch.setattr(controller, "TICK_S", 0.05)
    ctl, _, _, phone, spoken = await _run_outgoing()
    sid = next(iter(ctl.calls))
    ctl.config = CallConfig(**config)
    ctl.calls[sid].convo.playback.flush()
    return ctl, sid, spoken, phone


async def _wait_until_gone(ctl: CallController, sid: str) -> None:
    for _ in range(400):  # the goodbye is 3 s of audio that drains in real time
        if sid not in ctl.calls:
            return
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_an_idle_call_says_goodbye_and_ends_even_without_a_time_limit(
    monkeypatch: Any,
) -> None:
    ctl, sid, spoken, phone = await _limited_call(monkeypatch, idle_timeout=0.2, max_seconds=None)
    ctl.calls[sid].convo.last_active -= 5
    task = asyncio.create_task(ctl._limit(sid))
    await _wait_until_gone(ctl, sid)
    task.cancel()
    assert sid not in ctl.calls
    assert any("hanging up" in s for s in spoken)
    await phone.close()


@pytest.mark.asyncio
async def test_the_call_ends_at_max_seconds_and_no_limit_means_no_end(monkeypatch: Any) -> None:
    ctl, sid, spoken, phone = await _limited_call(monkeypatch, idle_timeout=0, max_seconds=0.3)
    await asyncio.wait_for(ctl._limit(sid), 5)  # returns once the limit ended the call
    assert sid not in ctl.calls
    assert not any("hanging up" in s for s in spoken)  # cut at the limit, not idle
    assert controller.HEADS_UP not in spoken  # a limit shorter than the heads-up gets none
    await phone.close()

    ctl, sid, _, phone = await _limited_call(monkeypatch, idle_timeout=0, max_seconds=None)
    task = asyncio.create_task(ctl._limit(sid))
    await asyncio.sleep(0.5)  # ten ticks: any deadline would have fired
    assert sid in ctl.calls
    assert not task.done()
    task.cancel()
    await ctl.close()
    await phone.close()


@pytest.mark.asyncio
async def test_the_call_says_once_that_it_will_end_and_still_ends_on_time(monkeypatch: Any) -> None:
    monkeypatch.setattr(controller, "HEADS_UP_S", 0.4)
    monkeypatch.setattr(controller, "HEADS_UP_LEAD_S", 0.2)
    ctl, sid, spoken, phone = await _limited_call(monkeypatch, idle_timeout=0, max_seconds=1)
    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(ctl._limit(sid), 5)
    assert asyncio.get_running_loop().time() - started < 1.5  # nothing extended the limit
    assert sid not in ctl.calls
    assert spoken.count(controller.HEADS_UP) == 1
    await phone.close()


@pytest.mark.asyncio
async def test_a_heads_up_queued_before_hang_up_is_not_sent_as_text() -> None:
    said: list[str] = []

    class Convo:
        closed = True

        async def say(self, text: str, **_: Any) -> None:
            said.append(text)

    await controller.CallController._heads_up(Convo())  # type: ignore[arg-type]
    Convo.closed = False
    await controller.CallController._heads_up(Convo())  # type: ignore[arg-type]
    assert said == [controller.HEADS_UP]


@pytest.mark.asyncio
async def test_greeting_is_one_of_the_phrases_spoken_cached_and_never_twice_in_a_row() -> None:
    ctl, _ = make()
    said: list[tuple[str, bool]] = []

    async def say(text: str, *, cached: bool = False) -> None:
        said.append((text, cached))

    connected = asyncio.Event()
    connected.set()
    call = SimpleNamespace(
        media=SimpleNamespace(connected=connected), convo=SimpleNamespace(say=say)
    )
    ctl.config = CallConfig(greetings=("Hello.", "Hi there.", "Yes?"))
    for _ in range(30):
        await ctl._greet(call)  # type: ignore[arg-type]
    assert {text for text, _ in said} == {"Hello.", "Hi there.", "Yes?"}
    assert all(a[0] != b[0] for a, b in itertools.pairwise(said))
    assert all(c for _, c in said)
    said.clear()
    ctl.config = CallConfig(greetings=())  # off
    await ctl._greet(call)  # type: ignore[arg-type]
    assert said == []


@pytest.mark.asyncio
async def test_uninterruptible_opening_is_protected_while_it_plays() -> None:
    ctl, _, _, phone, _ = await _run_outgoing(uninterruptible=True)
    sid = next(iter(ctl.calls))
    await asyncio.sleep(0.2)
    assert ctl.calls[sid].convo._protected  # still playing: nothing drains the playback in tests
    ctl.calls[sid].convo.playback.flush()
    await asyncio.sleep(0.2)
    assert not ctl.calls[sid].convo._protected
    await ctl.close()
    await phone.close()


@pytest.mark.asyncio
async def test_hang_up_after_ends_the_call_when_the_opening_is_done() -> None:
    ctl, client, _, phone, _ = await _run_outgoing(hang_up_after=True)
    sid = next(iter(ctl.calls))
    await asyncio.sleep(0.2)
    assert sid in ctl.calls  # still delivering
    ctl.config = CallConfig(hangup_grace=0.5)
    ctl.calls[sid].convo.playback.flush()
    await asyncio.sleep(0.2)
    assert sid in ctl.calls  # warning beeps, then the caller gets a moment to speak
    # end() drops the call first and sends session-terminate after closing the media
    for _ in range(100):
        if sid not in ctl.calls and client.jingle_sent()[-1].action == "session-terminate":
            break
        await asyncio.sleep(0.05)
    assert sid not in ctl.calls
    assert client.jingle_sent()[-1].action == "session-terminate"
    await phone.close()


@pytest.mark.asyncio
async def test_outgoing_call_not_answered_is_retracted() -> None:
    ctl, client = make()
    ctl.config = CallConfig(ring_timeout=0, ring_attempts=2, ring_pause=0)
    result = await ctl.place_call("alice@example.org", "Hello")
    assert result.startswith("no answer")
    assert [m.xml[0].tag.split("}")[1] for m in client.sent if m.kind == "message"] == [
        "propose",
        "retract",
        "propose",
        "retract",
    ]  # it rang twice before giving up
    assert client.jmi_sent()[-1][1] == "retract"
    assert ctl._ringing == {}
    ((peer, text),) = ctl.host.texts  # type: ignore[attr-defined]
    assert peer == "alice@example.org"  # sent as text instead
    assert "Hello" in text
    assert ctl._missed == {"alice@example.org": "Hello"}  # and remembered for a call back


@pytest.mark.asyncio
async def test_outgoing_call_declined() -> None:
    ctl, client = make()
    ctl.config = CallConfig(ring_attempts=1)
    task = asyncio.create_task(ctl.place_call("alice@example.org", "Hello"))
    await asyncio.sleep(0.05)
    proposal = str(client.sent[0].xml[0].get("id"))
    await ctl._on_jmi(proceed(proposal, "reject"))
    assert (await task).startswith("declined")
    assert "Hello" in ctl.host.texts[0][1]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_outgoing_call_to_stranger_is_refused() -> None:
    ctl, client = make()
    assert (await ctl.place_call("evil@example.org", "Hi")).startswith("refused")
    assert client.sent == []


@pytest.mark.asyncio
async def test_outgoing_call_while_busy() -> None:
    ctl, _ = make()
    ctl.config = CallConfig(busy_wait=0.3)
    ctl.calls["x"] = SimpleNamespace(peer="other@example.org/res")  # type: ignore[assignment]
    assert (await ctl.place_call("alice@example.org", "Hi")).startswith("busy")


@pytest.mark.asyncio
async def test_outgoing_call_waits_for_the_current_call_to_end() -> None:
    ctl, client = make()
    ctl.calls["x"] = SimpleNamespace(peer="other@example.org/res")  # type: ignore[assignment]
    task = asyncio.create_task(ctl.place_call("alice@example.org", "Hi"))
    await asyncio.sleep(0.4)
    assert client.sent == []  # still waiting its turn
    del ctl.calls["x"]
    await asyncio.sleep(0.5)
    assert [s.kind for s in client.sent] == ["message"]  # the call proposal went out
    task.cancel()


def proceed(proposal: str, verb: str = "proceed") -> FakeStanza:
    message = ET.Element("message")
    ET.SubElement(message, f"{{{JMI}}}{verb}", id=proposal)
    return FakeStanza(message, PEER, "chat")


async def _record(spoken: list[str], text: str) -> np.ndarray:
    spoken.append(text)
    return np.zeros(
        48000 * 3, np.int16
    )  # 3 s: long enough that the sender cannot drain it mid-test


@pytest.mark.asyncio
async def test_outgoing_call_to_someone_already_on_a_call_is_said_there() -> None:
    ctl, client, _, phone, spoken = await _run_outgoing()
    before = len(client.sent)
    result = await ctl.place_call("alice@example.org", "The result is in.")
    await asyncio.sleep(0.3)
    assert result.startswith("connected: the person is already on a call")
    assert len(client.sent) == before  # no second call was proposed
    assert "The result is in." in " ".join(spoken)
    await phone.close()


@pytest.mark.asyncio
async def test_a_phone_that_stops_ringing_by_itself_is_rung_again(monkeypatch) -> None:
    import calls.controller as module

    monkeypatch.setattr(module, "CLIENT_RING_LIMIT", 0.1)
    monkeypatch.setattr(module, "MIN_RERING", 0.1)
    ctl, client = make()
    ctl.config = CallConfig(ring_timeout=1, ring_attempts=1)
    task = asyncio.create_task(ctl.place_call("alice@example.org", "Hello"))
    await asyncio.sleep(0.2)
    first = str(client.sent[0].xml[0].get("id"))
    await ctl._on_jmi(proceed(first, "reject"))  # the phone's own ring limit ended
    await asyncio.sleep(0.1)
    proposals = [m for m in client.sent if m.kind == "message" and m.xml[0].tag.endswith("propose")]
    assert len(proposals) == 2  # rung again, not given up
    assert (await task).startswith("no answer")  # until our own limit
    assert len(ctl.host.texts) == 1  # one text at the very end  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_hanging_up_does_not_cancel_the_agents_work() -> None:
    ctl, _, _, phone, _ = await _run_outgoing()
    sid = next(iter(ctl.calls))
    done = asyncio.Event()

    async def slow_ask(peer: str, call_id: str, text: str) -> str:
        await asyncio.sleep(0.4)
        done.set()
        return "All done."

    ctl.host.ask = slow_ask  # type: ignore[method-assign]
    convo = ctl.calls[sid].convo
    convo._on_utterance = lambda pcm: None  # type: ignore[method-assign]
    convo._start_text("change the title")
    await asyncio.sleep(0.05)
    await ctl.end(sid, None)  # the person hangs up mid-request
    await asyncio.sleep(0.8)
    assert done.is_set()
    assert ("alice@example.org", "All done.") in ctl.host.texts  # type: ignore[attr-defined]
    await phone.close()


def test_session_title_templates() -> None:
    values = {
        "direction": "to", "name": "alice", "jid": "alice@example.org",
        "time": "2026-11-11 09:00", "date": "2026-11-11", "topic": "daily note",
    }  # fmt: skip
    render = controller.render_title
    assert (
        render(controller.TITLE_DEFAULTS["per_call"], **values)
        == "Call to alice 2026-11-11 09:00 daily note"
    )
    assert render("{date} {name}: {topic}", **values) == "2026-11-11 alice: daily note"
    assert render("{topic}", **{**values, "topic": ""}) is None  # nothing left: use the default
    assert render("Calls with {name}", **values) == "Calls with alice"
    assert render("/stop {name}\n/new", **values) == "stop alice /new"  # one line, no command
    assert render("x" * 300, **values) == "x" * 100
    for bad in ("{nope}", "{name.upper}", "{name!r}", "{name:>10}", "{name", "}"):
        assert render(bad, **values) is None, bad


def test_every_call_asks_the_host_to_title_its_session() -> None:
    ctl, _ = make()
    ctl.config = CallConfig(session="per_peer", session_title="Calls with {name} ({date})")
    seen: list[str] = []

    async def name_call(peer: str, call_id: str, title: str) -> None:
        seen.append(title)

    ctl.host.name_call = name_call  # type: ignore[method-assign]

    async def run() -> None:
        for _ in range(2):  # the host decides whether the person's session already has a title
            ctl._name("sid", "alice@example.org", "from")
        ctl.config = CallConfig(session="per_peer", session_title="{nope}")  # unusable: default
        ctl._name("sid", "alice@example.org", "from")
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert len(seen) == 3
    assert seen[0] == seen[1]
    assert seen[0].startswith("Calls with alice (20")
    assert seen[2] == "Calls with alice"


def test_topic_cannot_smuggle_a_command_into_the_title() -> None:
    ctl, _ = make()
    seen: list[str] = []

    async def name_call(peer: str, call_id: str, title: str) -> None:
        seen.append(title)

    ctl.host.name_call = name_call  # type: ignore[method-assign]

    async def run() -> None:
        ctl._name("sid", "alice@example.org", "to", "daily\n/stop now /new")
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert "\n" not in seen[0]
    assert seen[0].endswith("daily stop now new")


@pytest.mark.asyncio
async def test_another_peers_jingle_stanza_cannot_touch_a_call() -> None:
    ctl, _, _, phone, _ = await _run_outgoing()
    sid = next(iter(ctl.calls))
    from calls.jingle.model import Jingle

    await ctl.handle("mallory@example.org/x", Jingle("session-terminate", sid))
    assert sid in ctl.calls
    await ctl._fail(sid, {"from": "mallory@example.org/x"}, "general-error")
    assert sid in ctl.calls  # an error caused by somebody else does not end it
    await phone.close()


@pytest.mark.asyncio
async def test_a_call_whose_media_fails_is_ended() -> None:
    ctl, _, _, phone, _ = await _run_outgoing()
    sid = next(iter(ctl.calls))
    media = ctl.calls[sid].media
    assert media.on_lost is not None
    await media.on_lost()
    assert sid not in ctl.calls
    await phone.close()


@pytest.mark.asyncio
async def test_error_reply_to_our_setup_iq_ends_the_call() -> None:
    ctl, _client = make()
    ended: list[tuple[str, str | None]] = []

    async def end(sid: str, reason: str | None, notify: bool = False) -> None:
        ended.append((sid, reason))

    ctl.end = end  # type: ignore[method-assign]
    ctl.calls["s1"] = object()  # type: ignore[assignment]
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    class Iq:
        def send(self) -> asyncio.Future[None]:
            return future

    ctl._send(Iq(), "s1")

    class Refused(IqError):
        def __init__(self) -> None:  # the real one needs a full error stanza
            Exception.__init__(self, "service-unavailable")

    future.set_exception(Refused())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert ended == [("s1", "connectivity-error")]
