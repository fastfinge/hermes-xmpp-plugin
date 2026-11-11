"""Jingle session state machine (XEP-0166 section 7), audio calls only. Pure, no I/O.

`apply(event)` returns the next state or raises `IllegalTransition`; the caller does the I/O.
"""

from enum import Enum


class State(Enum):
    IDLE = "idle"  # propose seen or sent, no session yet (XEP-0353)
    PENDING = "pending"  # session-initiate sent or received, not accepted
    ACTIVE = "active"
    ENDED = "ended"


class IllegalTransition(Exception):
    pass


# (state, action) -> next state; who may send what is checked in apply()
_TRANSITIONS: dict[tuple[State, str], State] = {
    (State.IDLE, "session-initiate"): State.PENDING,
    (State.PENDING, "session-accept"): State.ACTIVE,
    (State.PENDING, "transport-info"): State.PENDING,
    (State.ACTIVE, "transport-info"): State.ACTIVE,
    (State.ACTIVE, "session-info"): State.ACTIVE,
    (State.PENDING, "session-info"): State.PENDING,
}


class Session:
    def __init__(self, sid: str, we_initiated: bool) -> None:
        self.sid = sid
        self.we_initiated = we_initiated
        self.state = State.IDLE

    def apply(self, action: str, *, sent_by_us: bool) -> State:
        """Move on `action`. `sent_by_us` tells who sent it; only the callee accepts."""
        if self.state is State.ENDED:
            raise IllegalTransition(f"{action} after session ended")
        if action == "session-terminate":
            self.state = State.ENDED
            return self.state
        if action == "session-initiate" and sent_by_us != self.we_initiated:
            raise IllegalTransition("session-initiate must come from the initiator")
        if action == "session-accept" and sent_by_us == self.we_initiated:
            raise IllegalTransition("session-accept must come from the responder")
        try:
            self.state = _TRANSITIONS[(self.state, action)]
        except KeyError:
            raise IllegalTransition(f"{action} not allowed in {self.state.value}") from None
        return self.state
