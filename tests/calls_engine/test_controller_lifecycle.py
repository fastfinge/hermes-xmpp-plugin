"""Offline regressions for signaling races and protocol replies."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from xml.etree import ElementTree as ET

import pytest
import slixmpp

import calls.controller as module
from .helpers import make_offer
from .test_controller import PEER, FakeHost, make, proceed, propose
from calls.controller import JMI, CallConfig, CallController
from calls.jingle import wire
from calls.jingle.model import Jingle


def iq(jingle, sender=PEER):
    stream = slixmpp.ClientXMPP("bot@example.org/res", "unused")
    replies = []
    cast(Any, stream).send = lambda stanza: replies.append(deepcopy(stanza.xml))
    stanza = slixmpp.Iq(stream=stream)
    stanza["from"], stanza["to"], stanza["type"] = sender, "bot@example.org/res", "set"
    stanza.xml.append(wire.build(jingle))
    return stanza, replies


@pytest.mark.parametrize("action", ["content-add", "transport-replace", "bogus"])
@pytest.mark.asyncio
async def test_unsupported_iq_action_gets_one_error_without_ending_call(action):
    ctl, client = make()
    caller, offer, _ = await make_offer("supported")
    try:
        await ctl.handle(PEER, offer)
        before = len(client.sent)
        stanza, replies = iq(Jingle(action, offer.sid))
        await ctl._on_jingle(stanza)
        assert len(replies) == 1
        assert replies[0].get("type") == "error"
        assert (
            replies[0].find(".//{urn:ietf:params:xml:ns:xmpp-stanzas}feature-not-implemented")
            is not None
        )
        assert offer.sid in ctl.calls
        assert len(client.sent) == before
    finally:
        await ctl.close()
        await caller.close()


@pytest.mark.parametrize("sender", [PEER, "stranger@example.org/res"])
@pytest.mark.asyncio
async def test_unknown_session_and_unauthorized_iq_get_one_error(sender):
    ctl, client = make()
    stanza, replies = iq(Jingle("transport-info", "missing"), sender)
    await ctl._on_jingle(stanza)
    assert len(replies) == 1
    assert replies[0].get("type") == "error"
    condition = "item-not-found" if sender == PEER else "service-unavailable"
    assert replies[0].find(f".//{{urn:ietf:params:xml:ns:xmpp-stanzas}}{condition}") is not None
    if sender == PEER:
        assert replies[0].find(".//{urn:xmpp:jingle:errors:1}unknown-session") is not None
    assert client.sent == []


@pytest.mark.asyncio
async def test_malformed_transport_iq_gets_one_error_without_ending_call():
    ctl, client = make()
    caller, offer, _ = await make_offer("malformed")
    try:
        await ctl.handle(PEER, offer)
        stanza, replies = iq(Jingle("transport-info", offer.sid, contents=offer.contents))
        transport = stanza.xml.find(".//{urn:xmpp:jingle:transports:ice-udp:1}transport")
        assert transport is not None
        ET.SubElement(
            transport, "{urn:xmpp:jingle:transports:ice-udp:1}candidate", port="not-an-integer"
        )
        before = len(client.sent)
        await ctl._on_jingle(stanza)
        assert len(replies) == 1
        assert replies[0].get("type") == "error"
        assert replies[0].find(".//{urn:ietf:params:xml:ns:xmpp-stanzas}bad-request") is not None
        assert offer.sid in ctl.calls
        assert len(client.sent) == before
    finally:
        await ctl.close()
        await caller.close()


@pytest.mark.asyncio
async def test_illegal_accept_gets_out_of_order_error_and_preserves_call():
    ctl, client = make()
    caller, offer, _ = await make_offer("active")
    try:
        await ctl.handle(PEER, offer)
        before = len(client.sent)
        stanza, replies = iq(Jingle("session-accept", offer.sid))
        await ctl._on_jingle(stanza)
        assert len(replies) == 1
        assert replies[0].get("type") == "error"
        assert replies[0].find(".//{urn:xmpp:jingle:errors:1}out-of-order") is not None
        assert offer.sid in ctl.calls
        assert len(client.sent) == before
    finally:
        await ctl.close()
        await caller.close()


@pytest.mark.parametrize("extra_media", ["video", "audio"])
@pytest.mark.asyncio
async def test_multi_content_offer_is_explicitly_refused(extra_media):
    ctl, client = make()
    caller, offer, _ = await make_offer("multi")
    extra = replace(offer.contents[0], name="video", media=extra_media)
    offer = replace(offer, contents=(*offer.contents, extra), group=("0", "video"))
    try:
        stanza, replies = iq(offer)
        await ctl._on_jingle(stanza)
        assert len(replies) == 1
        assert ctl.calls == {}
        assert [(j.action, j.reason) for j in client.jingle_sent()] == [
            ("session-terminate", "unsupported-applications")
        ]
    finally:
        await ctl.close()
        await caller.close()


@pytest.mark.parametrize("retract_first", [False, True])
@pytest.mark.asyncio
async def test_retracted_proposal_cannot_be_replayed_or_initiated(retract_first):
    ctl, client = make()
    caller, offer, _ = await make_offer("p1")
    try:
        if not retract_first:
            await ctl._on_jmi(propose())
        await ctl._on_jmi(proceed("p1", "retract"))
        before = len(client.sent)
        await ctl._on_jmi(propose())
        assert len(client.sent) == before
        await ctl.handle(PEER, offer)
        assert ctl.calls == {}
        assert not any(j.action == "session-accept" for j in client.jingle_sent())
    finally:
        await ctl.close()
        await caller.close()


@pytest.mark.parametrize(
    ("wrapper", "sender", "expected"),
    [
        ("received", "bot@example.org", ["accept", "proceed"]),
        ("received", "stranger@example.org", []),
        ("received", "bot@example.org/other-device", []),
        ("sent", "bot@example.org", []),
    ],
)
@pytest.mark.asyncio
async def test_received_jmi_carbons_require_authenticated_wrapper(
    monkeypatch, wrapper, sender, expected
):
    client = slixmpp.ClientXMPP("bot@example.org/res", "unused")
    ctl = CallController(client, FakeHost())
    ctl.install()
    sent = []
    monkeypatch.setattr(client, "send", lambda stanza: sent.append(deepcopy(stanza.xml)))
    message = slixmpp.Message(
        xml=ET.fromstring(
            f'<message xmlns="jabber:client" from="{sender}" type="chat">'
            f'<{wrapper} xmlns="urn:xmpp:carbons:2"><forwarded xmlns="urn:xmpp:forward:0">'
            f'<message xmlns="jabber:client" from="{PEER}" type="chat">'
            f'<propose xmlns="{JMI}" id="carbon"/>'
            f"</message></forwarded></{wrapper}></message>"
        ),
        stream=client,
    )
    handlers = [h for h in cast(Any, client)._XMLStream__handlers if h.name.startswith("calls-")]
    matched = [h for h in handlers if h.match(message)]
    if wrapper == "received":
        assert len(matched) == 1
    for handler in matched:
        await handler._pointer(message)
    assert [s[0].tag.rsplit("}", 1)[-1] for s in sent] == expected
    await ctl.close()


@pytest.mark.parametrize("malformation", ["missing-id", "video"])
@pytest.mark.asyncio
async def test_invalid_jmi_proposal_is_not_proceeded(malformation):
    ctl, client = make()
    message = propose()
    if malformation == "missing-id":
        message.xml[0].attrib.clear()
    else:
        ET.SubElement(message.xml[0], "{urn:xmpp:jingle:apps:rtp:1}description", media="video")
    await ctl._on_jmi(message)
    assert not any(verb in ("accept", "proceed") for _, verb in client.jmi_sent())


@pytest.mark.asyncio
async def test_old_carbon_forwarding_delay_does_not_ring():
    ctl, client = make()
    stamp = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    message = slixmpp.Message(
        xml=ET.fromstring(
            '<message xmlns="jabber:client" from="bot@example.org" type="chat">'
            '<received xmlns="urn:xmpp:carbons:2"><forwarded xmlns="urn:xmpp:forward:0">'
            f'<delay xmlns="urn:xmpp:delay" stamp="{stamp}"/>'
            f'<message xmlns="jabber:client" from="{PEER}" type="chat">'
            f'<propose xmlns="{JMI}" id="old-carbon"/>'
            "</message></forwarded></received></message>"
        )
    )
    await ctl._on_carbon(message)
    assert client.sent == []


@pytest.mark.asyncio
async def test_old_delayed_proposal_is_ignored():
    ctl, client = make()
    message = propose()
    stamp = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    ET.SubElement(message.xml, "{urn:xmpp:delay}delay", stamp=stamp)
    await ctl._on_jmi(message)
    assert client.sent == []


@pytest.mark.asyncio
async def test_proposal_tombstones_are_bounded():
    ctl, client = make()
    for index in range(1024):
        await ctl._on_jmi(proceed(str(index), "retract"))
    assert len(ctl._proposals) <= 256
    message = propose()
    message.xml[0].set("id", "1023")
    await ctl._on_jmi(message)
    assert client.sent == []


@pytest.mark.asyncio
async def test_duplicate_proposal_is_only_proceeded_once():
    ctl, client = make()
    await ctl._on_jmi(propose())
    await ctl._on_jmi(propose())
    assert [verb for _, verb in client.jmi_sent()] == ["accept", "proceed"]


@pytest.mark.asyncio
async def test_incoming_sid_cannot_collide_with_outgoing_proposal():
    ctl, client = make()
    ctl.config = CallConfig(max_calls=2)
    task = asyncio.create_task(ctl.place_call("alice@example.org", "Hello"))
    caller, offer, _ = await make_offer("collision")
    try:
        await eventually(lambda: ctl._ringing)
        sid = next(iter(ctl._ringing))
        await ctl.handle(PEER, replace(offer, sid=sid))
        assert ctl.calls == {}
        assert not any(j.action == "session-accept" for j in client.jingle_sent())
        assert len(ctl._ringing) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await ctl.close()
        await caller.close()


@pytest.mark.asyncio
async def test_duplicate_initiate_does_not_end_existing_call() -> None:
    ctl, client = make()
    caller, offer, _ = await make_offer("duplicate")
    try:
        await ctl.handle(PEER, offer)
        call = ctl.calls[offer.sid]
        before = len(client.sent)
        await ctl.handle(PEER, offer)
        assert ctl.calls.get(offer.sid) is call
        assert len(client.sent) == before
    finally:
        await ctl.close()
        await caller.close()


@pytest.mark.parametrize("signal", ["session-terminate", "transport-info"])
@pytest.mark.asyncio
async def test_incoming_signal_during_turn_fetch_is_not_lost(monkeypatch, signal) -> None:
    ctl, client = make()
    fetching, release = asyncio.Event(), asyncio.Event()

    async def fetch(_):
        fetching.set()
        await release.wait()
        return []

    monkeypatch.setattr(module.turn, "fetch", fetch)
    caller, offer, candidates = await make_offer("early")
    setup = asyncio.create_task(ctl.handle(PEER, offer))
    try:
        await asyncio.wait_for(fetching.wait(), 1)
        transport = offer.contents[0].transport
        assert transport is not None
        content = replace(
            offer.contents[0],
            media=None,
            payloads=(),
            transport=replace(transport, candidates=candidates),
        )
        await ctl.handle(
            PEER,
            Jingle(signal, offer.sid, contents=(content,), reason="cancel"),
        )
        release.set()
        await asyncio.wait_for(setup, 1)
        if signal == "session-terminate":
            assert ctl.calls == {}
            assert client.jingle_sent() == []
        else:
            assert ctl.calls[offer.sid].media._trickled == len(candidates)
            assert client.jingle_sent()[-1].action == "session-accept"
    finally:
        release.set()
        await asyncio.gather(setup, return_exceptions=True)
        await ctl.close()
        await caller.close()


@pytest.mark.asyncio
async def test_terminate_during_answer_releases_early_candidate_handler(monkeypatch):
    ctl, client = make()
    entered = asyncio.Event()

    async def answer(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module.CallMedia, "answer", answer)
    caller, offer, candidates = await make_offer("answering")
    setup = asyncio.create_task(ctl.handle(PEER, offer))
    candidate_task = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        transport = offer.contents[0].transport
        assert transport is not None
        content = replace(offer.contents[0], transport=replace(transport, candidates=candidates))
        candidate_task = asyncio.create_task(
            ctl.handle(PEER, Jingle("transport-info", offer.sid, contents=(content,)))
        )
        await asyncio.sleep(0)
        await ctl.handle(PEER, Jingle("session-terminate", offer.sid, reason="cancel"))
        await eventually(candidate_task.done)
        await candidate_task
        await setup
        assert ctl.calls == {}
        assert client.jingle_sent() == []
    finally:
        setup.cancel()
        if candidate_task is not None:
            candidate_task.cancel()
            await asyncio.gather(candidate_task, return_exceptions=True)
        await asyncio.gather(setup, return_exceptions=True)
        await ctl.close()
        await caller.close()


@pytest.mark.parametrize("stage", ["incoming-answer", "outgoing-turn", "outgoing-offer"])
@pytest.mark.asyncio
async def test_close_cancels_media_setups(monkeypatch, stage):
    ctl, client = make()
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    if stage == "outgoing-turn":
        monkeypatch.setattr(module.turn, "fetch", blocked)
    else:
        monkeypatch.setattr(
            module.CallMedia, "answer" if stage == "incoming-answer" else "offer", blocked
        )
    caller = None
    if stage == "incoming-answer":
        caller, offer, _ = await make_offer("setup-close")
        task = asyncio.create_task(ctl.handle(PEER, offer))
    else:
        task = asyncio.create_task(ctl.place_call("alice@example.org", "Hello"))
        await eventually(lambda: ctl._ringing)
        await ctl._on_jmi(proceed(next(iter(ctl._ringing))))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await ctl.close()
        await eventually(task.done)
        assert cancelled.is_set()
        assert ctl.calls == {}
        assert not ctl._busy()
        assert not any(
            j.action in ("session-accept", "session-initiate") for j in client.jingle_sent()
        )
        if stage != "incoming-answer":
            result = await task
            assert isinstance(result, str)
            assert result.startswith("closed")
        assert cast(FakeHost, ctl.host).texts == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await ctl.close()
        if caller is not None:
            await caller.close()


@pytest.mark.parametrize("waiting", ["busy", "pause"])
@pytest.mark.asyncio
async def test_close_wakes_outgoing_busy_and_retry_waits(waiting):
    ctl, client = make()
    caller = None
    if waiting == "busy":
        caller, offer, _ = await make_offer("occupied")
        await ctl.handle(PEER, offer)
        ctl.calls[offer.sid].peer = "other@example.org/phone"
    else:
        ctl.config = CallConfig(ring_timeout=0, ring_attempts=2, ring_pause=60)
    task = asyncio.create_task(ctl.place_call("alice@example.org", "Hello"))
    try:
        if waiting == "pause":
            await eventually(lambda: len(client.jmi_sent()) == 2)
        else:
            await asyncio.sleep(0)
        await ctl.close()
        await eventually(task.done)
        assert (await task).startswith("closed")
        assert len([v for _, v in client.jmi_sent() if v == "propose"]) <= 1
        assert cast(FakeHost, ctl.host).texts == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await ctl.close()
        if caller is not None:
            await caller.close()


@pytest.mark.asyncio
async def test_retry_checks_line_again_before_proposing():
    ctl, client = make()
    ctl.config = CallConfig(ring_timeout=0, ring_attempts=2, ring_pause=0.1)
    task = asyncio.create_task(ctl.place_call("alice@example.org", "Hello"))
    caller, offer, _ = await make_offer("occupied")
    try:
        await eventually(lambda: len(client.jmi_sent()) == 2)
        await ctl.handle(PEER, offer)
        result = await task
        assert result.startswith("busy")
        assert [v for _, v in client.jmi_sent()].count("propose") == 1
        assert set(ctl.calls) == {offer.sid}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await ctl.close()
        await caller.close()


@pytest.mark.asyncio
async def test_cancelling_outgoing_call_uses_xep_cancel_reason():
    ctl, client = make()
    task, _ = await outgoing(ctl, client)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert client.jingle_sent()[-1].reason == "cancel"
    assert ctl.calls == {}
    assert ctl._ringing == {}
    await ctl.close()


async def outgoing(ctl, client):
    task = asyncio.create_task(ctl.place_call("alice@example.org", "Hello"))
    await eventually(lambda: ctl._ringing)
    sid = next(iter(ctl._ringing))
    await ctl._on_jmi(proceed(sid))
    await eventually(lambda: any(j.action == "session-initiate" for j in client.jingle_sent()))
    return task, sid


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.asyncio
async def test_remote_terminate_wakes_outgoing_wait_without_missed_call(monkeypatch, accepted):
    ctl, client = make()

    async def accept(self, jingle):
        pass

    monkeypatch.setattr(module.CallMedia, "accept", accept)
    task, sid = await outgoing(ctl, client)
    try:
        if accepted:
            await ctl.handle(PEER, Jingle("session-accept", sid))
            await asyncio.sleep(0)
        await ctl.handle(PEER, Jingle("session-terminate", sid, reason="decline"))
        await eventually(task.done)
        result = await task
        assert result.startswith("terminated")
        assert "decline" in result
        assert "timeout" not in result
        assert "no answer" not in result
        assert cast(FakeHost, ctl.host).texts == []
        assert ctl.calls == {}
        assert not any(j.action == "session-terminate" for j in client.jingle_sent())
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await ctl.close()


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.asyncio
async def test_close_wakes_outgoing_acceptance_and_connection_waits(monkeypatch, accepted):
    ctl, client = make()

    async def accept(self, jingle):
        pass

    monkeypatch.setattr(module.CallMedia, "accept", accept)
    task, sid = await outgoing(ctl, client)
    try:
        if accepted:
            await ctl.handle(PEER, Jingle("session-accept", sid))
        await ctl.close()
        await eventually(task.done)
        assert (await task).startswith("closed")
        assert ctl.calls == {}
        assert ctl._ringing == {}
        assert cast(FakeHost, ctl.host).texts == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await ctl.close()


async def eventually(predicate) -> None:
    async with asyncio.timeout(1):
        while not predicate():  # noqa: ASYNC110  bounded polling of signaling state in tests
            await asyncio.sleep(0)


@pytest.mark.parametrize("ring_timeout", [0, 60])
@pytest.mark.asyncio
async def test_close_releases_ringing_and_ignores_late_proceed(ring_timeout) -> None:
    ctl, client = make()
    ctl.config = CallConfig(ring_timeout=ring_timeout)
    task = asyncio.create_task(ctl.place_call("alice@example.org", "Hello"))
    try:
        await eventually(lambda: ctl._ringing)
        sid = next(iter(ctl._ringing))
        answer = ctl._ringing[sid].answer
        await ctl.close()
        await ctl._on_jmi(proceed(sid))
        await eventually(task.done)
        assert (await task).startswith("closed")
        assert answer.done()
        assert ctl._ringing == {}
        assert ctl.calls == {}
        assert client.jingle_sent() == []
        assert client.jmi_sent()[-1][1] == "retract"
        assert cast(FakeHost, ctl.host).texts == []
        assert (await ctl.place_call("alice@example.org", "Later")).startswith("closed")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await ctl.close()


@pytest.mark.asyncio
async def test_close_during_incoming_turn_fetch_cancels_setup(monkeypatch) -> None:
    ctl, client = make()
    fetching = asyncio.Event()
    cancelled = asyncio.Event()

    async def fetch(_):
        fetching.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(module.turn, "fetch", fetch)
    caller, offer, _ = await make_offer("shutdown")
    setup = asyncio.create_task(ctl.handle(PEER, offer))
    try:
        await asyncio.wait_for(fetching.wait(), 1)
        await ctl.close()
        await eventually(setup.done)
        await setup
        assert cancelled.is_set()
        assert ctl.calls == {}
        assert not ctl._busy()
        assert not any(j.action == "session-accept" for j in client.jingle_sent())
        await ctl.handle(PEER, replace(offer, sid="after-close"))
        assert ctl.calls == {}
    finally:
        setup.cancel()
        await asyncio.gather(setup, return_exceptions=True)
        await ctl.close()
        await caller.close()
