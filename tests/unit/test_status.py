import pytest

from priorauth.status import TERMINAL, InvalidTransition, Status, check_transition

ALLOWED = [
    (Status.RECEIVED, Status.SENT),
    (Status.RECEIVED, Status.FAILED),
    (Status.SENT, Status.SENT),
    (Status.SENT, Status.APPROVED),
    (Status.SENT, Status.DENIED),
    (Status.SENT, Status.FAILED),
]


@pytest.mark.parametrize(("current", "target"), ALLOWED)
def test_allowed(current, target):
    check_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (Status.RECEIVED, Status.APPROVED),  # must be sent first
        (Status.RECEIVED, Status.RECEIVED),
        (Status.SENT, Status.RECEIVED),
        *[(t, s) for t in TERMINAL for s in Status],  # terminal states never move
    ],
)
def test_rejected(current, target):
    with pytest.raises(InvalidTransition):
        check_transition(current, target)
