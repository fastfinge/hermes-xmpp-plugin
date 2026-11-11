"""Call controller: Jingle Message Initiation + Jingle signaling for one slixmpp client, with
one `Conversation` per call. Host-agnostic: the Hermes plugin and the dev bots both implement
`Host`. Nothing here may raise into the host; a failing call is torn down and logged."""

import asyncio
import logging
import re
import uuid
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from xml.etree import ElementTree as ET

from slixmpp.exceptions import IqError, XMPPError

from . import turn
from .audio import Endpointer, webrtc_vad
from .conversation import FILLERS, Conversation, Speech
from .jingle import wire
from .jingle.model import JINGLE, RTP, Candidate, Jingle, JingleError
from .jingle.sdp import MAX_CANDIDATES
from .jingle.session import IllegalTransition, Session
from .media import CallMedia
from .speech import clean, split_hangup

JMI = "urn:xmpp:jingle-message:0"
RTP_INFO = "urn:xmpp:jingle:apps:rtp:info:1"
HINTS = "urn:xmpp:hints"
CALL_FEATURES = (
    JINGLE,
    "urn:xmpp:jingle:transports:ice-udp:1",
    "urn:xmpp:jingle:apps:rtp:1",
    "urn:xmpp:jingle:apps:dtls:0",
    "urn:xmpp:jingle:apps:rtp:audio",
    JMI,
)


RECAP_PROMPT = (
    "[The call has ended. In one or two plain sentences for a chat message, say what the person "
    "asked for or wanted changed during the call and what you did about it. If they asked for "
    "nothing, reply exactly NONE.]"
)
CONNECT_LIMIT = 40.0  # a call whose media is not up after this long is dead
IDLE_GOODBYE = "I do not hear anyone, so I am hanging up. Call me back any time."
CLIENT_RING_LIMIT = 25.0  # a "reject" after ringing this long is the phone timing out
MIN_RERING = 5.0  # do not ring again with less than this left of the ring time
MISSED_CALL = "I tried to call you but nobody picked up. What I wanted to say:\n\n{opening}"


class Host(Protocol):
    """What the controller needs from whoever embeds it."""

    @property
    def speech(self) -> Speech: ...

    def authorized(self, bare_jid: str) -> bool: ...

    async def ask(self, peer: str, call_id: str, text: str) -> str:
        """One agent turn for `text` said by `peer` (bare JID) on call `call_id`."""
        ...

    async def post_text(self, peer: str, text: str) -> None:
        """Send `text` to the peer's chat (a reply that could not be spoken)."""
        ...

    async def steer(self, peer: str, call_id: str, text: str) -> bool:
        """Give `text` to the agent turn already running for this call. False if not possible."""
        ...

    async def name_call(self, peer: str, call_id: str, title: str) -> None:
        """Give the call's agent session a recognizable title. Never raises."""
        ...


@dataclass(frozen=True)
class CallConfig:
    max_seconds: int = 600
    greeting: str = "Hi, I'm here. What can I do for you?"
    filler_after: float = 3.0
    fillers: tuple[str, ...] = FILLERS  # one is said at random when the agent is slow; () is off
    filler_cache: Path | None = None  # where its audio is kept between calls (None: not kept)
    max_calls: int = 1
    ring_timeout: int = 60  # outgoing calls: seconds to ring before giving up
    ring_attempts: int = 1  # rings before the message goes out as text (urgent calls: more)
    ring_pause: float = 60.0  # seconds between rings
    silence_ms: int = 800  # this much non-speech ends the caller's turn
    vad_level: int = (
        2  # WebRTC VAD aggressiveness 0-3: higher rejects more noise, misses soft speech
    )
    ack_tone: bool = True  # short beep when an utterance is captured
    hangup_grace: float = 5.0  # warning beeps, then this long to speak before a planned hang-up
    busy_wait: float = 90.0  # outgoing calls wait this long for the current call to end
    idle_timeout: float = 60.0  # nobody has spoken for this long: say goodbye and hang up (0: off)
    log_transcripts: bool = False  # log what was said (default: only how many words)
    recap: bool = True  # after a call with talk in it, text the person what was asked and done
    barge_in: str = (
        "queue"  # "queue": talk any time, it becomes the next turn; "stop": cut the agent off
    )


class Call:
    def __init__(self, peer: str, session: Session, media: CallMedia, convo: Conversation) -> None:
        self.peer, self.session, self.media, self.convo = peer, session, media, convo
        self.tasks: list[asyncio.Task[None]] = []
        self.accepted = asyncio.Event()  # outgoing: the peer sent session-accept
        self.ended: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def wait(self, event: asyncio.Event, limit: float) -> bool:
        waiter = asyncio.create_task(event.wait())
        try:
            async with asyncio.timeout(limit):
                await asyncio.wait((waiter, self.ended), return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        return not self.ended.done()


def _bare(jid: str) -> str:
    return jid.split("/", 1)[0].lower()


class Ringing:
    """An outgoing call proposed and not yet answered."""

    def __init__(self, peer: str) -> None:
        self.peer = peer
        self.answer: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()


@dataclass
class Pending:
    peer: str
    session: Session
    candidates: list[Candidate] = field(default_factory=list)
    task: asyncio.Task[Any] | None = None
    reason: str = "success"


class CallController:
    def __init__(self, client: Any, host: Host, config: CallConfig | None = None) -> None:
        self.client, self.host, self.config = client, host, config or CallConfig()
        self.calls: dict[str, Call] = {}
        self._ringing: dict[str, Ringing] = {}
        self._background: set[asyncio.Task[None]] = set()
        self._pending: dict[str, Pending] = {}
        self._closed = asyncio.Event()
        self._proposals: dict[tuple[str, str], str] = {}
        self._missed: dict[str, str] = {}  # peer -> text sent instead of a call nobody answered

    # ---- wiring -------------------------------------------------------------------------

    def install(self) -> None:
        """Register stanza handlers. Call once, before the client connects."""
        from slixmpp.xmlstream.handler import CoroutineCallback
        from slixmpp.xmlstream.matcher import MatchXPath

        ns = self.client.default_ns
        for verb in ("propose", "retract", "proceed", "reject"):
            path = f"{{{ns}}}message/{{{JMI}}}{verb}"
            self.client.register_handler(
                CoroutineCallback(f"calls-{verb}", MatchXPath(path), self._on_jmi)
            )
        self.client.register_handler(
            CoroutineCallback(
                "calls-carbon-received",
                MatchXPath(f"{{{ns}}}message/{{urn:xmpp:carbons:2}}received"),
                self._on_carbon,
            )
        )
        path = f"{{{ns}}}iq/{{{JINGLE}}}jingle"
        self.client.register_handler(
            CoroutineCallback("calls-jingle", MatchXPath(path), self._on_jingle)
        )

    async def advertise(self) -> None:
        """Publish call features. After session bind: slixmpp keys features by the current JID."""
        disco = self.client.plugin["xep_0030"]
        for feature in CALL_FEATURES:
            disco.add_feature(feature)
        if "xep_0115" in self.client.plugin:
            await self.client.plugin["xep_0115"].update_caps()

    async def close(self) -> None:
        self._closed.set()
        for sid, ringing in list(self._ringing.items()):
            self._jmi(ringing.peer, "retract", sid)
            if not ringing.answer.done():
                ringing.answer.set_result(None)
        self._ringing.clear()
        tasks = [p.task for p in self._pending.values() if p.task is not None]
        for sid in list(set(self.calls) | set(self._pending)):
            await self.end(sid, "success", notify=True)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ---- Jingle Message Initiation --------------------------------------------------------

    async def _on_carbon(self, stanza: Any) -> None:
        from slixmpp.stanza import Message

        if str(stanza["from"]) != self.client.boundjid.bare or stanza["type"] == "error":
            return
        if stanza.xml.find("{urn:xmpp:carbons:2}sent") is not None:
            return
        forwarded = stanza.xml.find("{urn:xmpp:carbons:2}received/{urn:xmpp:forward:0}forwarded")
        if forwarded is None:
            return
        message = forwarded.find(f"{{{self.client.default_ns}}}message")
        if message is not None:
            inner = Message(xml=deepcopy(message))
            for delay in forwarded.findall("{urn:xmpp:delay}delay"):
                inner.xml.append(deepcopy(delay))
            await self._on_jmi(inner)

    async def _on_jmi(self, stanza: Any) -> None:
        try:
            if self._closed.is_set():
                return
            child = next((c for c in stanza.xml if c.tag.startswith(f"{{{JMI}}}")), None)
            peer = stanza["from"]
            if child is None or stanza["type"] == "error" or not self.host.authorized(peer.bare):
                return  # an error is a bounce of our own propose, not a call for us
            verb, proposal_id = child.tag.rsplit("}", 1)[1], child.get("id", "")
            if not proposal_id:
                return
            if verb == "propose":
                if any(c.get("media") != "audio" for c in child.findall(f"{{{RTP}}}description")):
                    self._remember_proposal(peer.bare, proposal_id, "retracted")
                    self._jmi(peer.bare, "reject", proposal_id)
                    return
                for delay in stanza.xml.findall("{urn:xmpp:delay}delay"):
                    try:
                        stamp = datetime.fromisoformat(delay.get("stamp", ""))
                        age = (datetime.now(UTC) - stamp).total_seconds()
                    except (ValueError, TypeError):
                        return
                    if age > self.config.ring_timeout:
                        return
                self._answer_proposal(peer, proposal_id)
            elif verb == "retract":
                self._remember_proposal(peer.bare, proposal_id, "retracted")
                pending = self._pending.get(proposal_id)
                if pending is not None and _bare(pending.peer) == _bare(str(peer)):
                    await self.end(proposal_id, "cancel")
            elif verb in ("proceed", "reject"):
                self._callee_replied(peer, verb, proposal_id)
        except Exception:
            logging.exception("calls: JMI handling failed")

    def _remember_proposal(self, peer: str, sid: str, state: str) -> None:
        self._proposals[(_bare(peer), sid)] = state
        # shortcut: remember the last 256 IDs; persist only if replay survives this window.
        if len(self._proposals) > 256:
            del self._proposals[next(iter(self._proposals))]

    def _answer_proposal(self, peer: Any, proposal_id: str) -> None:
        if (_bare(str(peer)), proposal_id) in self._proposals:
            return
        self._remember_proposal(str(peer), proposal_id, "proposed")
        if self._busy():
            self._jmi(peer.bare, "reject", proposal_id)
            return
        # accept to ourselves stops our other devices ringing; proceed tells the caller to offer.
        self._jmi(self.client.boundjid.bare, "accept", proposal_id)
        self._jmi(peer, "proceed", proposal_id)

    def _callee_replied(self, peer: Any, verb: str, proposal_id: str) -> None:
        ringing = self._ringing.get(proposal_id)
        if ringing is None or ringing.peer != peer.bare or ringing.answer.done():
            return
        ringing.answer.set_result(str(peer) if verb == "proceed" else None)

    def _busy(self) -> bool:
        return (
            len(set(self.calls) | set(self._pending) | set(self._ringing)) >= self.config.max_calls
        )

    def _send(self, iq: Any, sid: str | None = None) -> None:
        """Fire an IQ set without waiting; a sleeping phone that never answers is not an error.
        An error reply to a call's own setup IQ (`sid`) ends that call."""

        def done(future: Any) -> None:
            if future.cancelled() or (error := future.exception()) is None:
                return
            logging.info("calls: no good answer to our IQ: %s", type(error).__name__)
            if sid is not None and isinstance(error, IqError) and sid in self.calls:
                task = asyncio.create_task(self.end(sid, "connectivity-error"))
                self._background.add(task)
                task.add_done_callback(self._background.discard)

        pending = iq.send()
        if hasattr(pending, "add_done_callback"):  # the fake clients in tests send nothing
            pending.add_done_callback(done)

    def _jmi(self, to: Any, verb: str, proposal_id: str, *, propose: bool = False) -> None:
        message = self.client.make_message(mto=to, mtype="chat")
        element = ET.SubElement(message.xml, f"{{{JMI}}}{verb}", id=proposal_id)
        if propose:
            ET.SubElement(element, f"{{{RTP}}}description", media="audio")
            ET.SubElement(message.xml, f"{{{HINTS}}}store")  # offline devices still get the call
        message.send()

    # ---- Jingle ------------------------------------------------------------------------------

    async def _on_jingle(self, stanza: Any) -> None:
        element = stanza.xml.find(f"{{{JINGLE}}}jingle")
        if element is None or stanza["type"] != "set":
            return
        sid, action = element.get("sid", ""), element.get("action", "")
        try:
            if self._closed.is_set() or not self.host.authorized(stanza["from"].bare):
                raise XMPPError("service-unavailable")
            if not sid or not action:
                raise XMPPError("bad-request")
            if action not in (
                "session-initiate",
                "session-accept",
                "session-terminate",
                "transport-info",
                "session-info",
            ):
                raise XMPPError("feature-not-implemented")
            call = self.calls.get(sid) or self._pending.get(sid)
            if call is not None and _bare(call.peer) != _bare(str(stanza["from"])):
                raise XMPPError(
                    "item-not-found",
                    extension="unknown-session",
                    extension_ns="urn:xmpp:jingle:errors:1",
                )
            if action != "session-initiate" and call is None:
                raise XMPPError(
                    "item-not-found",
                    extension="unknown-session",
                    extension_ns="urn:xmpp:jingle:errors:1",
                )
            if action == "session-initiate" and (call is not None or sid in self._ringing):
                raise IllegalTransition("session-initiate for an existing session")
            try:
                jingle = wire.parse(element)
            except JingleError as error:
                raise XMPPError("bad-request") from error
            await self.handle(str(stanza["from"]), jingle)
        except XMPPError as error:
            stanza.exception(error)
            return
        except JingleError as error:
            logging.warning("calls: refusing call %s: %s", sid, error)
            await self._fail(sid, stanza, error.reason)
        except IllegalTransition as error:
            logging.warning("calls: illegal transition on %s: %s", sid, error)
            stanza.exception(
                XMPPError(
                    "unexpected-request",
                    extension="out-of-order",
                    extension_ns="urn:xmpp:jingle:errors:1",
                )
            )
            return
        except Exception:
            logging.exception("calls: call %s failed", sid)
            await self._fail(sid, stanza, "general-error")
            stanza.exception(XMPPError("internal-server-error"))
            return
        stanza.reply().send()

    async def _fail(self, sid: str, stanza: Any, reason: str) -> None:
        """End the call this stanza was about, but never somebody else's call with the same sid."""
        call = self.calls.get(sid)
        sender = str(stanza["from"])
        if call is None or _bare(call.peer) == _bare(sender):
            await self.end(sid, reason, notify=sender)

    async def handle(self, peer: str, jingle: Jingle) -> None:
        if jingle.action == "session-initiate":
            await self._incoming(peer, jingle)
            return
        call = self.calls.get(jingle.sid)
        pending = self._pending.get(jingle.sid)
        if (
            pending is not None
            and _bare(pending.peer) == _bare(peer)
            and (
                call is None
                or (jingle.action == "transport-info" and not pending.session.we_initiated)
            )
        ):
            pending.session.apply(jingle.action, sent_by_us=False)
            if jingle.action == "session-terminate":
                await self.end(jingle.sid, jingle.reason)
            elif jingle.action == "transport-info":
                for content in jingle.contents:
                    for candidate in content.transport.candidates if content.transport else ():
                        if len(pending.candidates) < MAX_CANDIDATES:
                            pending.candidates.append(candidate)
            return
        if call is None or _bare(call.peer) != _bare(peer):
            return  # unknown sid, or somebody else's call
        call.session.apply(jingle.action, sent_by_us=False)
        if jingle.action == "session-info":
            return self._session_info(call, jingle)
        if jingle.action == "session-accept":
            await call.media.accept(jingle)
            call.accepted.set()
        elif jingle.action == "transport-info":
            for content in jingle.contents:
                for candidate in content.transport.candidates if content.transport else ():
                    await call.media.add_candidate(candidate)
        elif jingle.action == "session-terminate":
            await self.end(jingle.sid, jingle.reason)

    def _session_info(self, call: Call, jingle: Jingle) -> None:
        if jingle.info == "mute":
            call.convo.caller_muted()

    async def _incoming(self, peer: str, jingle: Jingle) -> None:
        if self._closed.is_set():
            return
        if jingle.sid in self.calls or jingle.sid in self._pending or jingle.sid in self._ringing:
            return  # a repeated sid never replaces or ends the call that has it
        if self._proposals.get((_bare(peer), jingle.sid)) in ("retracted", "ended"):
            await self.end(jingle.sid, "cancel", notify=peer)
            return
        if len(jingle.contents) != 1 or jingle.contents[0].media != "audio":
            await self.end(jingle.sid, "unsupported-applications", notify=peer)
            return
        if self._busy():
            await self.end(jingle.sid, "busy", notify=peer)
            return
        session = Session(jingle.sid, we_initiated=False)
        session.apply("session-initiate", sent_by_us=False)
        pending = self._pending[jingle.sid] = Pending(peer, session)
        pending.task = asyncio.create_task(self._start_incoming(peer, jingle, pending))
        try:
            await pending.task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                await self.end(jingle.sid, "cancel", notify=True)
                raise
        finally:
            self._pending.pop(jingle.sid, None)

    async def _start_incoming(self, peer: str, jingle: Jingle, pending: Pending) -> None:
        servers = await turn.fetch(self.client)
        session = pending.session
        bare = peer.split("/", 1)[0]
        convo = self._conversation(jingle.sid, bare)
        if bare in self._missed:
            convo.remember_missed(self._missed.pop(bare))
        self._name(jingle.sid, bare, "from")
        media = CallMedia(servers, make_outgoing=convo.attach)
        call = self.calls[jingle.sid] = Call(peer, session, media, convo)
        media.on_lost = lambda: self.end(jingle.sid, "connectivity-error", notify=True)
        content = await media.answer(jingle)
        for candidate in pending.candidates:
            await media.add_candidate(candidate)
        self._pending.pop(jingle.sid, None)
        session.apply("session-accept", sent_by_us=True)
        accept = Jingle("session-accept", jingle.sid, contents=(content,), group=jingle.group)
        self._send(self.client.make_iq_set(sub=wire.build(accept), ito=peer), jingle.sid)
        call.tasks += [
            asyncio.create_task(self._limit(jingle.sid)),
            asyncio.create_task(self._greet(call)),
        ]

    def _conversation(self, sid: str, bare: str) -> Conversation:
        config = self.config
        return Conversation(
            self.host.speech,
            lambda text: self.host.ask(bare, sid, text),
            lambda: self.end(sid, "success", notify=True),
            filler_after=config.filler_after,
            fillers=config.fillers,
            filler_cache=config.filler_cache,
            endpointer=Endpointer(
                silence_ms=config.silence_ms, is_speech=webrtc_vad(config.vad_level)
            ),
            ack_tone_enabled=config.ack_tone,
            fallback_text=lambda text: self.host.post_text(bare, text),
            barge_in=config.barge_in,
            hangup_grace=config.hangup_grace,
            log_text=config.log_transcripts,
            steer=lambda text: self.host.steer(bare, sid, text),
        )

    def _name(self, sid: str, bare: str, direction: str, topic: str = "") -> None:
        """Title the call's session: "Call from|to <person> <YYYY-MM-DD HH:MM> [<topic>]"."""
        when = datetime.now().strftime("%Y-%m-%d %H:%M")
        # One plain line: the title travels inside a /title command.
        topic = " ".join(re.sub(r"[^\w .:@-]", " ", topic).split())[:60]
        title = f"Call {direction} {bare.split('@', 1)[0]} {when}" + (f" {topic}" if topic else "")
        task = asyncio.create_task(self.host.name_call(bare, sid, title))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _after_call(self, call: Call, sid: str) -> None:
        """Hanging up cancels nothing the person asked: finish their last turn, then recap."""
        try:
            await call.convo.finish()
        except Exception:
            logging.exception("calls: finishing the last turn of %s failed", sid)
        if self.config.recap and call.convo.turns:
            await self._recap(call.peer.split("/", 1)[0], sid)

    async def _recap(self, bare: str, sid: str) -> None:
        """After the call: ask the call's own agent session what was asked, and text the answer."""
        try:
            reply = clean(split_hangup(await self.host.ask(bare, sid, RECAP_PROMPT))[0]).strip()
            if reply and reply.strip(" .").upper() != "NONE":
                await self.host.post_text(bare, f"Call recap: {reply}")
        except Exception:
            logging.exception("calls: the call recap failed")

    async def _missed_call(self, peer: str, opening: str) -> None:
        """Nobody answered: send the message as text and remember it for a call back."""
        bare = peer.split("/", 1)[0]
        self._missed[bare] = opening
        try:
            await self.host.post_text(bare, MISSED_CALL.format(opening=opening))
        except Exception:
            logging.exception("calls: sending the missed-call text failed")

    # ---- outgoing calls ------------------------------------------------------------------

    async def place_call(
        self,
        peer: str,
        opening: str,
        *,
        uninterruptible: bool = False,
        hang_up_after: bool = False,
        topic: str = "",
        context: str = "",
        attempts: int | None = None,
        pause: float | None = None,
    ) -> str:
        """Call `peer` (bare JID) and, when answered, say `opening`. Returns a status sentence.

        `uninterruptible`: the opening is delivered whole whatever the person says meanwhile.
        `hang_up_after`: end the call when the opening is done instead of taking questions.

        Never raises. The conversation then runs on its own; this returns once the call is up
        or has failed, so an agent tool can report the outcome.
        """
        if self._closed.is_set():
            return "closed: the call controller is shut down"
        if not self.host.authorized(peer):
            return f"refused: {peer} is not allowed to be called"
        for sid, call in self.calls.items():
            if call.peer.split("/", 1)[0] == peer:
                # They are on a call with us right now: say it there, do not ring them again.
                call.convo.remember(opening, context)
                task = asyncio.create_task(
                    self._deliver(sid, call.convo, opening, uninterruptible, hang_up_after)
                )
                self._background.add(task)
                task.add_done_callback(self._background.discard)
                return "connected: the person is already on a call with you; it is being said there"
        deadline = asyncio.get_running_loop().time() + self.config.busy_wait
        # A result can arrive just as the previous call ends: queue behind it instead of failing.
        while self._busy() and asyncio.get_running_loop().time() < deadline:
            try:
                await asyncio.wait_for(self._closed.wait(), 0.25)
                break
            except TimeoutError:
                pass
        if self._closed.is_set():
            return "closed: the call controller is shut down"
        if self._busy():
            return "busy: another call is in progress"
        outcome = ""
        tries = max(1, attempts if attempts is not None else self.config.ring_attempts)
        wait = self.config.ring_pause if pause is None else pause
        loop = asyncio.get_running_loop()
        for attempt in range(tries):
            if attempt:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._closed.wait(), wait)
            deadline = loop.time() + self.config.ring_timeout
            while True:
                if self._closed.is_set():
                    return "closed: the call controller is shut down"
                if self._busy():
                    return "busy: another call is in progress"
                proposal = str(uuid.uuid4())
                ringing = self._ringing[proposal] = Ringing(peer)
                asked = loop.time()
                try:
                    self._jmi(peer, "propose", proposal, propose=True)
                    try:
                        full = await asyncio.wait_for(
                            ringing.answer, max(0.0, deadline - loop.time())
                        )
                    except TimeoutError:
                        if self._closed.is_set():
                            return "closed: the call controller is shut down"
                        self._jmi(peer, "retract", proposal)
                        outcome = "no answer: nobody picked up in time"
                        break
                    if self._closed.is_set():
                        return "closed: the call controller is shut down"
                    if full is None:
                        outcome = "declined: the person rejected the call"
                        # A phone gives up ringing on its own after about half a minute: that
                        # is not a refusal, so ring again until our own limit.
                        if (
                            loop.time() - asked >= CLIENT_RING_LIMIT
                            and deadline - loop.time() > MIN_RERING
                        ):
                            continue
                        break
                    result = await self._offer(
                        full, proposal, opening, uninterruptible, hang_up_after, topic, context
                    )
                    return (
                        "closed: the call controller is shut down"
                        if self._closed.is_set()
                        else result
                    )
                except asyncio.CancelledError:  # the agent tool gave up: leave nothing ringing
                    self._jmi(peer, "retract", proposal)
                    await self.end(proposal, "cancel", notify=True)
                    raise
                except Exception as error:
                    logging.exception("calls: outgoing call to %s failed", peer)
                    await self.end(proposal, "general-error", notify=True)
                    return f"failed: {error}"
                finally:
                    self._ringing.pop(proposal, None)
        await self._missed_call(peer, opening)
        return f"{outcome}; the message was sent as text instead"

    async def _offer(
        self,
        full: str,
        sid: str,
        opening: str,
        uninterruptible: bool,
        hang_up_after: bool,
        topic: str = "",
        context: str = "",
    ) -> str:
        pending = self._pending[sid] = Pending(full, Session(sid, we_initiated=True))
        pending.task = asyncio.create_task(self._start_outgoing(sid, pending, opening, context))
        try:
            call = await pending.task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            return (
                "closed: the call controller is shut down"
                if self._closed.is_set()
                else f"terminated: {pending.reason}"
            )
        finally:
            self._pending.pop(sid, None)
        media, convo = call.media, call.convo
        try:
            if not await call.wait(call.accepted, self.config.ring_timeout):
                return f"terminated: {call.ended.result()}"
        except TimeoutError:
            await self.end(sid, "timeout", notify=True)
            await self._missed_call(full, opening)
            return "no answer: the device never accepted the call; message sent as text instead"
        try:
            if not await call.wait(media.connected, self.config.ring_timeout):
                return f"terminated: {call.ended.result()}"
        except TimeoutError:
            await self.end(sid, "connectivity-error", notify=True)
            return "failed: the call was accepted but audio never connected"
        self._name(sid, full.split("/", 1)[0], "to", topic)
        call.tasks.append(asyncio.create_task(self._limit(sid)))
        call.tasks.append(
            asyncio.create_task(self._deliver(sid, convo, opening, uninterruptible, hang_up_after))
        )
        return "connected: the call is up and the opening is being spoken"

    async def _start_outgoing(self, sid: str, pending: Pending, opening: str, context: str) -> Call:
        servers = await turn.fetch(self.client)
        full, session = pending.peer, pending.session
        convo = self._conversation(sid, full.split("/", 1)[0])
        convo.remember(opening, context)
        media = CallMedia(servers, make_outgoing=convo.attach)
        call = self.calls[sid] = Call(full, session, media, convo)
        media.on_lost = lambda: self.end(sid, "connectivity-error", notify=True)
        convo.prepare(opening)  # synthesized while the phone rings
        content = await media.offer()
        session.apply("session-initiate", sent_by_us=True)
        initiate = Jingle(
            "session-initiate",
            sid,
            initiator=str(self.client.boundjid),
            contents=(content,),
            group=(content.name,),
        )
        self._send(self.client.make_iq_set(sub=wire.build(initiate), ito=full), sid)
        return call

    async def _deliver(
        self,
        sid: str,
        convo: Conversation,
        opening: str,
        uninterruptible: bool,
        hang_up_after: bool,
    ) -> None:
        try:
            await convo.say(opening, protected=uninterruptible)
        except Exception:
            logging.exception("calls: delivering the opening of %s failed", sid)
            return
        if hang_up_after:
            await convo.playback.wait_idle()
            if not await convo.grace(self.config.hangup_grace):
                await self.end(sid, "success", notify=True)

    async def _greet(self, call: Call) -> None:
        await call.media.connected.wait()
        if self.config.greeting:
            await call.convo.say(self.config.greeting)

    async def _limit(self, sid: str) -> None:
        """Enforce the call time limit; meanwhile log RTP counts every 10 s (is audio flowing?)."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self.config.max_seconds
        while (left := deadline - loop.time()) > 0:
            await asyncio.sleep(min(10, left))
            call = self.calls.get(sid)
            if call is not None:
                logging.info("calls: %s rtp received/sent %s", sid[:8], await call.media.packets())
                if not call.media.connected.is_set() and loop.time() - started > CONNECT_LIMIT:
                    logging.info("calls: %s never connected, ending", sid[:8])
                    break
                if self.config.idle_timeout and call.convo.idle_for() > self.config.idle_timeout:
                    logging.info("calls: %s idle, hanging up", sid[:8])
                    await call.convo.say(IDLE_GOODBYE)
                    await call.convo.playback.wait_idle()
                    break
        await self.end(sid, "success", notify=True)

    async def end(self, sid: str, reason: str | None, notify: str | bool = False) -> None:
        """Close one call; with `reason`, tell the peer (`notify`: its JID, or True)."""
        call = self.calls.pop(sid, None)
        pending = self._pending.pop(sid, None)
        if pending is not None:
            pending.reason = reason or "success"
            if pending.task is not None and pending.task is not asyncio.current_task():
                pending.task.cancel()
        target = call or pending
        if target is not None:
            self._remember_proposal(target.peer, sid, "ended")
        logging.info("calls: ending %s (%s)", sid[:8], reason)
        peer = notify if isinstance(notify, str) else (target.peer if target and notify else None)
        if call is not None:
            call.ended.set_result(reason or "success")
            task = asyncio.create_task(self._after_call(call, sid))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
            for task in call.tasks:
                if task is not asyncio.current_task():
                    task.cancel()
            try:
                await call.media.close()
            except Exception:
                logging.exception("calls: closing media of %s failed", sid)
        if reason and peer:
            terminate = wire.build(Jingle("session-terminate", sid, reason=reason))
            self._send(self.client.make_iq_set(sub=terminate, ito=peer))
