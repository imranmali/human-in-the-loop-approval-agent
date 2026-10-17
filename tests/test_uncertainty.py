from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from hitl.core import Action, Event, ManualClock, NotFoundError, RiskLevel, ValidationError
from hitl.uncertainty import (
    Assessment,
    ConfidenceSignal,
    DisagreementSignal,
    NoveltySignal,
    RiskSignal,
    RuleSignal,
    Signal,
    SignalResult,
    UncertaintyDetector,
    default_detector,
)

EMPTY: Mapping[str, Any] = {}


class Fixed:
    """A test signal returning a fixed score."""

    def __init__(self, name: str, score: float, *, force: bool = False, reason: str | None = None) -> None:
        self.name = name
        self.score = score
        self.force = force
        self.reason = reason if reason is not None else f"{name} reason"

    def evaluate(self, action: Action, context: Mapping[str, Any]) -> SignalResult:
        return SignalResult(self.name, self.score, self.reason, self.force)


class Boom:
    name = "boom"

    def evaluate(self, action: Action, context: Mapping[str, Any]) -> SignalResult:
        raise RuntimeError("kaboom")


class Liar:
    name = "liar"

    def evaluate(self, action: Action, context: Mapping[str, Any]) -> Any:
        return 0.0


def act(name: str = "send_email", **kw: Any) -> Action:
    return Action(name, **kw)


# ----------------------------------------------------------------------------------- signal results


def test_signal_result_valid() -> None:
    r = SignalResult("risk", 0.5, "risk MEDIUM")
    assert (r.name, r.score, r.reason, r.forces_review) == ("risk", 0.5, "risk MEDIUM", False)
    assert isinstance(r.score, float)
    with pytest.raises(AttributeError):
        r.score = 0.1  # type: ignore[misc]


@pytest.mark.parametrize("bad", [1.5, -0.1, float("nan"), "0.5", True, None])
def test_signal_result_rejects_bad_score(bad: Any) -> None:
    with pytest.raises(ValidationError):
        SignalResult("x", bad, "r")


def test_builtin_signals_satisfy_protocol() -> None:
    for s in (ConfidenceSignal(), RiskSignal(), NoveltySignal(), DisagreementSignal()):
        assert isinstance(s, Signal)


# ----------------------------------------------------------------------------------------- confidence


def test_confidence_low_scored_and_explained() -> None:
    r = ConfidenceSignal(threshold=0.7).evaluate(act(confidence=0.4), EMPTY)
    assert r.score == pytest.approx(0.6)
    assert "below threshold" in r.reason
    assert not r.forces_review


def test_confidence_floor_forces_review() -> None:
    r = ConfidenceSignal(floor=0.5).evaluate(act(confidence=0.3), EMPTY)
    assert r.forces_review
    assert "below threshold" in r.reason and "floor" in r.reason


def test_confidence_high_passes() -> None:
    r = ConfidenceSignal(floor=0.5).evaluate(act(confidence=0.95), EMPTY)
    assert r.score == pytest.approx(0.05)
    assert not r.forces_review
    assert "below" not in r.reason


def test_confidence_rejects_bad_config() -> None:
    with pytest.raises(ValidationError):
        ConfidenceSignal(threshold=2)
    with pytest.raises(ValidationError):
        ConfidenceSignal(floor=-0.5)


# ----------------------------------------------------------------------------------------------- risk


def test_risk_mapped_to_score() -> None:
    sig = RiskSignal()
    scores = [sig.evaluate(act(risk=level), EMPTY).score for level in RiskLevel]
    assert scores == [0.0, 0.33, 0.67, 1.0]


def test_risk_at_review_level_forces_review() -> None:
    sig = RiskSignal(review_at=RiskLevel.HIGH)
    forced = {level: sig.evaluate(act(risk=level), EMPTY).forces_review for level in RiskLevel}
    assert forced == {
        RiskLevel.LOW: False,
        RiskLevel.MEDIUM: False,
        RiskLevel.HIGH: True,
        RiskLevel.CRITICAL: True,
    }


def test_risk_custom_scores_and_validation() -> None:
    sig = RiskSignal(RiskLevel.CRITICAL, scores={RiskLevel.MEDIUM: 0.5})
    assert sig.evaluate(act(risk=RiskLevel.MEDIUM), EMPTY).score == 0.5
    assert not sig.evaluate(act(risk=RiskLevel.HIGH), EMPTY).forces_review
    with pytest.raises(ValidationError):
        RiskSignal(review_at=9)
    with pytest.raises(ValidationError):
        RiskSignal(scores={RiskLevel.LOW: 3.0})


# ---------------------------------------------------------------------------------------------- rules


def _amount_rule(**kw: Any) -> RuleSignal:
    return RuleSignal(
        "large-amount",
        lambda a, c: a.params.get("amount", 0) > 10_000,
        "amount > 10000",
        **kw,
    )


def test_rule_match_forces_review() -> None:
    r = _amount_rule().evaluate(act("transfer", params={"amount": 25_000}), EMPTY)
    assert (r.score, r.reason, r.forces_review) == (1.0, "amount > 10000", True)


def test_rule_no_match_passes() -> None:
    r = _amount_rule().evaluate(act("transfer", params={"amount": 50}), EMPTY)
    assert r.score == 0.0 and not r.forces_review


def test_soft_rule_contributes_score_only() -> None:
    r = _amount_rule(score=0.4, force=False).evaluate(act("transfer", params={"amount": 20_000}), EMPTY)
    assert r.score == 0.4 and not r.forces_review


def test_rule_always_review_list_and_validation() -> None:
    rule = RuleSignal.always_review(["delete_db", "wire_money"])
    assert rule.evaluate(act("delete_db"), EMPTY).forces_review
    assert not rule.evaluate(act("read_db"), EMPTY).forces_review
    with pytest.raises(ValidationError):
        RuleSignal("Bad Name", lambda a, c: True, "r")
    with pytest.raises(ValidationError):
        RuleSignal("ok", "not callable", "r")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        RuleSignal("ok", lambda a, c: True, "")


def test_rule_uses_context() -> None:
    rule = RuleSignal("prod", lambda a, c: c.get("env") == "prod", "production environment")
    det = UncertaintyDetector([rule])
    assert det.assess(act(), {"env": "prod"}).requires_review
    assert not det.assess(act(), {"env": "dev"}).requires_review


# -------------------------------------------------------------------------------------------- novelty


def test_novelty_unseen_is_novel() -> None:
    r = NoveltySignal().evaluate(act(params={"to": "a"}), EMPTY)
    assert r.score == 1.0
    assert r.reason.startswith("novel action")


def test_novelty_observed_shape_is_familiar_regardless_of_values() -> None:
    sig = NoveltySignal()
    sig.observe(act("transfer", params={"amount": 10, "to": "x"}))
    assert sig.evaluate(act("transfer", params={"to": "y", "amount": 20}), EMPTY).score == 0.0
    # different keys or name are a different shape
    assert sig.evaluate(act("transfer", params={"amount": 10}), EMPTY).score == 1.0
    assert sig.evaluate(act("refund", params={"amount": 10, "to": "x"}), EMPTY).score == 1.0


def test_novelty_min_observations() -> None:
    sig = NoveltySignal(min_observations=2)
    a = act()
    assert sig.observe(a) == 1
    assert sig.evaluate(a, EMPTY).score == 1.0
    assert sig.observe(a) == 2
    assert sig.evaluate(a, EMPTY).score == 0.0
    assert sig.observations(a) == 2


def test_novelty_bounded_lru_eviction() -> None:
    sig = NoveltySignal(max_tracked=3)
    actions = [act(f"op{i}") for i in range(4)]
    for a in actions[:3]:
        sig.observe(a)
    # touching op0 makes op1 the least recently used
    assert sig.evaluate(actions[0], EMPTY).score == 0.0
    sig.observe(actions[3])
    assert len(sig) == 3
    assert sig.evaluate(actions[1], EMPTY).score == 1.0
    for a in (actions[0], actions[2], actions[3]):
        assert sig.evaluate(a, EMPTY).score == 0.0
    for i in range(100):
        sig.observe(act(f"bulk{i}"))
    assert len(sig) == 3


def test_novelty_forget_clear_and_validation() -> None:
    sig = NoveltySignal()
    a = act()
    sig.observe(a)
    assert sig.forget(a) is True
    assert sig.forget(a) is False
    sig.observe(a)
    sig.clear()
    assert len(sig) == 0
    with pytest.raises(ValidationError):
        NoveltySignal(min_observations=0)
    with pytest.raises(ValidationError):
        NoveltySignal(max_tracked=0)


# --------------------------------------------------------------------------------------- disagreement


def test_disagreement_unanimous() -> None:
    ctx = {"candidates": [{"a": 1, "b": 2}, {"b": 2, "a": 1}, {"a": 1, "b": 2}]}
    assert DisagreementSignal().evaluate(act(), ctx).score == 0.0


def test_disagreement_split() -> None:
    r = DisagreementSignal().evaluate(act(), {"candidates": ["a", "a", "b", "c"]})
    assert r.score == pytest.approx(0.5)
    assert "2/4" in r.reason


@pytest.mark.parametrize("ctx", [{}, {"candidates": ["only"]}, {"candidates": []}])
def test_disagreement_too_few(ctx: dict[str, Any]) -> None:
    assert DisagreementSignal().evaluate(act(), ctx).score == 0.0


def test_disagreement_malformed() -> None:
    with pytest.raises(ValidationError):
        DisagreementSignal().evaluate(act(), {"candidates": "abc"})


def test_disagreement_custom_key_in_detector() -> None:
    det = UncertaintyDetector([DisagreementSignal(key="alts")], threshold=0.6)
    a = det.assess(act(), {"alts": ["x", "y", "z"]})
    assert a.score == pytest.approx(2 / 3)
    assert a.requires_review
    # malformed context is fail-safe
    assert det.assess(act(), {"alts": 5}).requires_review


# ------------------------------------------------------------------------------------- configuration


@pytest.mark.parametrize(
    "kwargs",
    [
        {"aggregation": "median"},
        {"threshold": 1.2},
        {"threshold": -0.1},
        {"signals": [Fixed("risk", 0.1), Fixed("risk", 0.2)]},
        {"weights": {"risk": -1}},
        {"weights": {"risk": float("inf")}},
        {"signals": [Fixed("Bad Name", 0.1)]},
    ],
)
def test_invalid_configuration_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        UncertaintyDetector(**kwargs)


# --------------------------------------------------------------------------------------- aggregation


def test_max_aggregation() -> None:
    det = UncertaintyDetector([Fixed("a", 0.2), Fixed("b", 0.6)], threshold=0.9)
    assert det.assess(act()).score == pytest.approx(0.6)


def test_weighted_aggregation() -> None:
    det = UncertaintyDetector(
        [Fixed("a", 1.0), Fixed("b", 0.0)], aggregation="weighted", weights={"a": 3, "b": 1}, threshold=0.9
    )
    assert det.assess(act()).score == pytest.approx(0.75)


def test_weighted_zero_total_weight() -> None:
    det = UncertaintyDetector([Fixed("a", 1.0)], aggregation="weighted", weights={"a": 0})
    assert det.assess(act()).score == 0.0


def test_noisy_or_aggregation() -> None:
    det = UncertaintyDetector([Fixed("a", 0.5), Fixed("b", 0.5)], aggregation="noisy_or", threshold=0.9)
    assert det.assess(act()).score == pytest.approx(0.75)


def test_weights_scale_and_cap_in_max() -> None:
    det = UncertaintyDetector([Fixed("a", 0.4), Fixed("b", 0.3)], weights={"a": 0.5, "b": 10})
    assert det.assess(act()).score == pytest.approx(1.0)


def test_no_signals_scores_zero() -> None:
    a = UncertaintyDetector().assess(act(risk=RiskLevel.CRITICAL, confidence=0.0))
    assert a.score == 0.0 and not a.requires_review and a.reasons == ()


# --------------------------------------------------------------------------------- decision & output


def test_score_at_threshold_requires_review() -> None:
    assert UncertaintyDetector([Fixed("a", 0.5)], threshold=0.5).assess(act()).requires_review
    assert not UncertaintyDetector([Fixed("a", 0.49)], threshold=0.5).assess(act()).requires_review


def test_forcing_signal_overrides_low_score() -> None:
    det = UncertaintyDetector(
        [Fixed("a", 0.1, force=True), Fixed("b", 0.0), Fixed("c", 0.0)], aggregation="weighted"
    )
    a = det.assess(act())
    assert a.score < a.threshold
    assert a.requires_review


def test_reasons_ordered_by_score() -> None:
    det = UncertaintyDetector([Fixed("low", 0.3), Fixed("high", 0.9), Fixed("zero", 0.0)])
    a = det.assess(act())
    assert a.reasons == ("high reason", "low reason")
    assert [r.name for r in a.signal_results] == ["low", "high", "zero"]


def test_forcing_zero_score_signal_included_in_reasons() -> None:
    a = UncertaintyDetector([Fixed("veto", 0.0, force=True, reason="")]).assess(act())
    assert a.requires_review
    assert a.reasons == ("signal veto scored 0.00",)


def test_assessment_fields_and_serialisable() -> None:
    clock = ManualClock(1000.0)
    action = act(params={"to": "bob"}, risk=RiskLevel.MEDIUM, confidence=0.4)
    det = UncertaintyDetector([ConfidenceSignal(), RiskSignal()], clock=clock)
    a = det.assess(action)
    assert isinstance(a, Assessment)
    assert a.id.startswith("asm_")
    assert a.action_fingerprint == action.fingerprint()
    assert a.risk is RiskLevel.MEDIUM
    assert a.created_at == 1000.0
    assert a.threshold == 0.5
    d = json.loads(json.dumps(a.to_dict()))
    assert d["id"] == a.id
    assert d["score"] == pytest.approx(0.6)
    assert d["requires_review"] is True
    assert d["reasons"][0].startswith("confidence 0.40")
    assert {r["name"] for r in d["signal_results"]} == {"confidence", "risk"}
    assert d["risk"] == "MEDIUM"
    assert det.assess(action).id != a.id


def test_invalid_assess_inputs() -> None:
    det = UncertaintyDetector()
    with pytest.raises(ValidationError):
        det.assess("not an action")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        det.assess(act(), ["not", "a", "mapping"])  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------ fail safe


def test_raising_signal_forces_review() -> None:
    a = UncertaintyDetector([Fixed("ok", 0.0), Boom()]).assess(act())
    assert a.requires_review
    assert a.reasons[0].startswith("signal boom failed")
    assert "kaboom" in a.reasons[0]
    failed = next(r for r in a.signal_results if r.name == "boom")
    assert failed.score == 1.0 and failed.forces_review


def test_invalid_return_forces_review() -> None:
    a = UncertaintyDetector([Liar()], aggregation="weighted", weights={"liar": 0}).assess(act())
    assert a.requires_review
    assert a.reasons[0].startswith("signal liar failed")


def test_result_name_normalised_to_signal_name() -> None:
    class Misnamed:
        name = "real"

        def evaluate(self, action: Action, context: Mapping[str, Any]) -> SignalResult:
            return SignalResult("other", 0.2, "r")

    a = UncertaintyDetector([Misnamed()]).assess(act())
    assert a.signal_results[0].name == "real"


# --------------------------------------------------------------------------------- runtime management


def test_add_and_remove_signals() -> None:
    det = UncertaintyDetector([Fixed("a", 0.1)])
    det.add_signal(Fixed("b", 0.9))
    assert [r.name for r in det.assess(act()).signal_results] == ["a", "b"]
    assert det.get_signal("b").name == "b"
    removed = det.remove_signal("b")
    assert removed.name == "b"
    a = det.assess(act())
    assert [r.name for r in a.signal_results] == ["a"]
    assert not a.requires_review


def test_invalid_management_operations() -> None:
    det = UncertaintyDetector([Fixed("a", 0.1)])
    with pytest.raises(ValidationError):
        det.add_signal(Fixed("a", 0.2))
    with pytest.raises(NotFoundError):
        det.remove_signal("missing")
    with pytest.raises(NotFoundError):
        det.get_signal("missing")

    class NoEvaluate:
        name = "noeval"

    with pytest.raises(ValidationError):
        det.add_signal(NoEvaluate())  # type: ignore[arg-type]


def test_detector_observe_forwards_to_learning_signals() -> None:
    det = UncertaintyDetector([NoveltySignal(), Fixed("a", 0.0)])
    assert det.assess(act()).requires_review
    det.observe(act())
    assert not det.assess(act()).requires_review


# -------------------------------------------------------------------------------------------- batches


async def test_batch_preserves_order() -> None:
    det = UncertaintyDetector([DisagreementSignal()], threshold=0.5)
    items = [act("op_a"), (act("op_b"), {"candidates": ["x", "y"]}), (act("op_c"), None)]
    out = await det.assess_many(items)
    assert [a.action_name for a in out] == ["op_a", "op_b", "op_c"]
    assert [a.requires_review for a in out] == [False, True, False]


async def test_batch_rejects_malformed_item() -> None:
    with pytest.raises(ValidationError):
        await UncertaintyDetector().assess_many([(act(), {}, "extra")])  # type: ignore[list-item]


# --------------------------------------------------------------------------------------------- events


def test_events_for_reviewed_action() -> None:
    events: list[Event] = []
    clock = ManualClock(42.0)
    det = UncertaintyDetector([RiskSignal()], clock=clock, event_sink=events.append)
    a = det.assess(act("wire_money", risk=RiskLevel.CRITICAL))
    assert [e.kind for e in events] == ["uncertainty.assessed", "uncertainty.review_required"]
    for e in events:
        assert e.subject_id == a.id
        assert e.timestamp == 42.0
        assert e.data["action"] == "wire_money"
        assert e.data["score"] == 1.0
        assert e.data["requires_review"] is True
        assert e.data["reasons"] == list(a.reasons)


def test_events_for_passing_action() -> None:
    events: list[Event] = []
    det = UncertaintyDetector([RiskSignal()], clock=ManualClock(), event_sink=events.append)
    det.assess(act())
    assert [e.kind for e in events] == ["uncertainty.assessed"]
    assert events[0].data["requires_review"] is False


def test_failing_sink_isolated() -> None:
    def sink(event: Event) -> None:
        raise RuntimeError("audit down")

    a = UncertaintyDetector([RiskSignal()], event_sink=sink).assess(act(risk=RiskLevel.HIGH))
    assert a.requires_review


# -------------------------------------------------------------------------------------------- default


def test_default_detector_behaviour() -> None:
    events: list[Event] = []
    det = default_detector(clock=ManualClock(), event_sink=events.append)
    assert [s.name for s in det.signals] == ["confidence", "risk", "novelty"]
    safe = act("read_file", params={"path": "/a"}, confidence=0.95)
    first = det.assess(safe)
    assert first.requires_review
    assert any(r.startswith("novel action") for r in first.reasons)
    det.observe(safe)
    assert not det.assess(act("read_file", params={"path": "/b"}, confidence=0.95)).requires_review
    critical = act("read_file", params={"path": "/c"}, risk=RiskLevel.CRITICAL, confidence=0.95)
    assert det.assess(critical).requires_review
    low_conf = act("read_file", params={"path": "/d"}, confidence=0.45)
    assert det.assess(low_conf).requires_review


def test_default_detector_overrides() -> None:
    det = default_detector(threshold=0.9, aggregation="noisy_or", extra_signals=[DisagreementSignal()])
    assert det.threshold == 0.9 and det.aggregation == "noisy_or"
    assert det.signals[-1].name == "disagreement"
