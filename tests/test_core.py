import pytest

from hitl.core import (
    Action,
    Event,
    EventBus,
    ManualClock,
    RiskLevel,
    ValidationError,
    canonical_json,
    fingerprint,
    new_id,
)


def test_manual_clock_advances_and_rejects_negative():
    clock = ManualClock(10)
    assert clock.now() == 10
    clock.advance(5)
    assert clock.now() == 15
    with pytest.raises(ValidationError):
        clock.advance(-1)


def test_new_id_is_prefixed_and_unique():
    a, b = new_id("apr"), new_id("apr")
    assert a.startswith("apr_") and a != b
    with pytest.raises(ValidationError):
        new_id("Bad Prefix")


def test_fingerprint_is_order_independent():
    assert fingerprint({"a": 1, "b": [1, 2]}) == fingerprint({"b": [1, 2], "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})
    assert canonical_json({"r": RiskLevel.HIGH}) == '{"r":3}'


def test_action_validation_and_coercion():
    action = Action("payments.refund", {"amount": 10}, risk=3, confidence=0.4)
    assert action.risk is RiskLevel.HIGH
    assert action.to_dict()["risk"] == "HIGH"
    assert (
        action.fingerprint() == Action("payments.refund", {"amount": 10}, RiskLevel.HIGH, 0.4).fingerprint()
    )
    with pytest.raises(ValidationError):
        Action("Bad Name")
    with pytest.raises(ValidationError):
        Action("ok", confidence=1.5)


def test_event_bus_isolates_failing_sinks():
    received = []
    bus = EventBus()

    def broken(event):
        raise RuntimeError("boom")

    bus.subscribe(broken)
    unsubscribe = bus.subscribe(received.append)
    event = Event("approval.requested", "apr_1")
    bus.emit(event)
    assert received == [event]
    unsubscribe()
    bus(event)
    assert received == [event]
    with pytest.raises(ValidationError):
        Event("approval.requested", "")
