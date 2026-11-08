"""The request lifecycle. Every change goes through `check_transition` and is written to status_history."""

from enum import StrEnum


class Status(StrEnum):
    RECEIVED = "received"
    SENT = "sent"
    APPROVED = "approved"
    DENIED = "denied"
    FAILED = "failed"


TERMINAL = frozenset({Status.APPROVED, Status.DENIED, Status.FAILED})

# SENT -> SENT is a retry: the attempt failed and the request goes out again after a delay.
ALLOWED: dict[Status, frozenset[Status]] = {
    Status.RECEIVED: frozenset({Status.SENT, Status.FAILED}),
    Status.SENT: frozenset({Status.SENT, Status.APPROVED, Status.DENIED, Status.FAILED}),
    Status.APPROVED: frozenset(),
    Status.DENIED: frozenset(),
    Status.FAILED: frozenset(),
}


class InvalidTransition(Exception):
    def __init__(self, current: Status, target: Status):
        super().__init__(f"cannot move a request from {current} to {target}")
        self.current = current
        self.target = target


def check_transition(current: Status, target: Status) -> None:
    if target not in ALLOWED[current]:
        raise InvalidTransition(current, target)
