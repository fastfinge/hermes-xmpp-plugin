import pytest

from calls.jingle.session import IllegalTransition, Session, State


def incoming() -> Session:
    return Session("s1", we_initiated=False)


def test_incoming_call_happy_path() -> None:
    s = incoming()
    assert s.apply("session-initiate", sent_by_us=False) is State.PENDING
    assert s.apply("transport-info", sent_by_us=False) is State.PENDING
    assert s.apply("session-accept", sent_by_us=True) is State.ACTIVE
    assert s.apply("transport-info", sent_by_us=False) is State.ACTIVE
    assert s.apply("session-terminate", sent_by_us=False) is State.ENDED


def test_outgoing_call_happy_path() -> None:
    s = Session("s2", we_initiated=True)
    assert s.apply("session-initiate", sent_by_us=True) is State.PENDING
    assert s.apply("session-accept", sent_by_us=False) is State.ACTIVE


def test_decline_from_pending() -> None:
    s = incoming()
    s.apply("session-initiate", sent_by_us=False)
    assert s.apply("session-terminate", sent_by_us=True) is State.ENDED


@pytest.mark.parametrize(
    ("action", "sent_by_us"),
    [("session-accept", True), ("transport-info", False), ("session-initiate", True)],
)
def test_illegal_from_idle(action: str, sent_by_us: bool) -> None:
    with pytest.raises(IllegalTransition):
        incoming().apply(action, sent_by_us=sent_by_us)


def test_callee_cannot_accept_its_own_offer() -> None:
    s = Session("s3", we_initiated=True)
    s.apply("session-initiate", sent_by_us=True)
    with pytest.raises(IllegalTransition):
        s.apply("session-accept", sent_by_us=True)


def test_nothing_after_end() -> None:
    s = incoming()
    s.apply("session-terminate", sent_by_us=False)
    with pytest.raises(IllegalTransition):
        s.apply("transport-info", sent_by_us=False)
