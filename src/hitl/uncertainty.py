"""Uncertainty detection: decide when the agent must pause and ask a human.

Signals score a proposed :class:`~hitl.core.Action` in ``[0, 1]`` and may *force* review outright.
An :class:`UncertaintyDetector` aggregates those scores, compares them with a threshold and returns an
immutable, explainable :class:`Assessment`. Signal failures are fail-safe: they force review.
"""

from __future__ import annotations

import asyncio
import math
import threading
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Literal, Protocol, runtime_checkable

from hitl.core import (
    Action,
    Clock,
    Event,
    EventSink,
    NotFoundError,
    RiskLevel,
    SystemClock,
    ValidationError,
    fingerprint,
    logger,
    new_id,
    null_sink,
    validate_name,
)

__all__ = [
    "AGGREGATIONS",
    "Assessment",
    "ConfidenceSignal",
    "DisagreementSignal",
    "NoveltySignal",
    "RiskSignal",
    "RuleSignal",
    "Signal",
    "SignalResult",
    "UncertaintyDetector",
    "default_detector",
]

Aggregation = Literal["max", "weighted", "noisy_or"]
AGGREGATIONS: frozenset[str] = frozenset({"max", "weighted", "noisy_or"})

EVENT_ASSESSED = "uncertainty.assessed"
EVENT_REVIEW_REQUIRED = "uncertainty.review_required"


def _unit_interval(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value):
        raise ValidationError(f"{what} must be a number within [0, 1], got {value!r}")
    if not 0.0 <= float(value) <= 1.0:
        raise ValidationError(f"{what} must be within [0, 1], got {value!r}")
    return float(value)


# --------------------------------------------------------------------------------------------- signals


@dataclass(frozen=True)
class SignalResult:
    """The verdict of one signal: a score in ``[0, 1]``, an explanation and an optional veto."""

    name: str
    score: float
    reason: str = ""
    forces_review: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValidationError("signal result name must be a non-empty string")
        object.__setattr__(self, "score", _unit_interval(self.score, f"signal {self.name!r} score"))
        if not isinstance(self.reason, str):
            raise ValidationError(f"signal {self.name!r} reason must be a string")
        object.__setattr__(self, "forces_review", bool(self.forces_review))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": self.score,
            "reason": self.reason,
            "forces_review": self.forces_review,
        }


@runtime_checkable
class Signal(Protocol):
    """Anything with a unique ``name`` that can score an action in context."""

    name: str

    def evaluate(self, action: Action, context: Mapping[str, Any]) -> SignalResult: ...


class ConfidenceSignal:
    """Scores ``1 - confidence``.

    ``threshold`` only shapes the explanation ("below threshold"); the review decision is made by the
    detector threshold. If ``floor`` is set, confidence strictly below it forces review.
    """

    def __init__(
        self, threshold: float = 0.7, *, floor: float | None = None, name: str = "confidence"
    ) -> None:
        self.name = validate_name(name, "signal name")
        self.threshold = _unit_interval(threshold, "confidence threshold")
        self.floor = None if floor is None else _unit_interval(floor, "confidence floor")

    def evaluate(self, action: Action, context: Mapping[str, Any]) -> SignalResult:
        confidence = float(action.confidence)
        forced = self.floor is not None and confidence < self.floor
        reason = f"confidence {confidence:.2f}"
        if confidence < self.threshold:
            reason += f" below threshold {self.threshold:.2f}"
        if forced:
            reason += f" (below floor {self.floor:.2f})"
        return SignalResult(self.name, max(0.0, min(1.0, 1.0 - confidence)), reason, forced)


DEFAULT_RISK_SCORES: Mapping[RiskLevel, float] = MappingProxyType(
    {RiskLevel.LOW: 0.0, RiskLevel.MEDIUM: 0.33, RiskLevel.HIGH: 0.67, RiskLevel.CRITICAL: 1.0}
)


class RiskSignal:
    """Maps the declared :class:`RiskLevel` to a score; risk at or above ``review_at`` forces review."""

    def __init__(
        self,
        review_at: RiskLevel | int = RiskLevel.HIGH,
        *,
        scores: Mapping[RiskLevel, float] | None = None,
        name: str = "risk",
    ) -> None:
        self.name = validate_name(name, "signal name")
        try:
            self.review_at = RiskLevel(review_at)
        except ValueError as exc:
            raise ValidationError(f"invalid review_at risk level {review_at!r}") from exc
        merged = dict(DEFAULT_RISK_SCORES)
        for level, score in (scores or {}).items():
            try:
                merged[RiskLevel(level)] = _unit_interval(score, f"risk score for {level!r}")
            except ValueError as exc:
                if isinstance(exc, ValidationError):
                    raise
                raise ValidationError(f"invalid risk level {level!r}") from exc
        self.scores: Mapping[RiskLevel, float] = MappingProxyType(merged)

    def evaluate(self, action: Action, context: Mapping[str, Any]) -> SignalResult:
        risk = action.risk
        forced = risk >= self.review_at
        reason = f"risk {risk.name}" + (f" at or above review level {self.review_at.name}" if forced else "")
        return SignalResult(self.name, self.scores[risk], reason, forced)


Predicate = Callable[[Action, Mapping[str, Any]], bool]


class RuleSignal:
    """A policy rule: when ``predicate(action, context)`` holds, report ``score`` and ``reason``.

    A matching rule forces review unless ``force=False``. Example::

        RuleSignal("large-amount", lambda a, c: a.params.get("amount", 0) > 10_000, "amount > 10000")

    Use :meth:`always_review` for an action deny/always-review list.
    """

    def __init__(
        self, name: str, predicate: Predicate, reason: str, *, score: float = 1.0, force: bool = True
    ) -> None:
        self.name = validate_name(name, "signal name")
        if not callable(predicate):
            raise ValidationError(f"rule {name!r}: predicate must be callable")
        if not isinstance(reason, str) or not reason:
            raise ValidationError(f"rule {name!r}: reason must be a non-empty string")
        self.predicate = predicate
        self.reason = reason
        self.score = _unit_interval(score, f"rule {name!r} score")
        self.force = bool(force)

    def evaluate(self, action: Action, context: Mapping[str, Any]) -> SignalResult:
        if self.predicate(action, context):
            return SignalResult(self.name, self.score, self.reason, self.force)
        return SignalResult(self.name, 0.0, "")

    @classmethod
    def always_review(cls, action_names: Iterable[str], *, name: str = "always-review") -> RuleSignal:
        """A rule forcing review for every action whose name is in ``action_names``."""
        names = frozenset(action_names)
        return cls(
            name,
            lambda action, _ctx: action.name in names,
            "action is on the always-review list",
        )


class NoveltySignal:
    """Flags action *shapes* (name + sorted param keys) seen fewer than ``min_observations`` times.

    Memory is bounded to ``max_tracked`` shapes with least-recently-used eviction. Call :meth:`observe`
    when an action should count as familiar (for example after it was approved or executed).
    """

    def __init__(
        self, min_observations: int = 1, *, max_tracked: int = 10_000, name: str = "novelty"
    ) -> None:
        self.name = validate_name(name, "signal name")
        for what, value in (("min_observations", min_observations), ("max_tracked", max_tracked)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValidationError(f"{what} must be an integer >= 1, got {value!r}")
        self.min_observations = min_observations
        self.max_tracked = max_tracked
        self._seen: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def shape(action: Action) -> str:
        """The fingerprint of an action's shape; parameter values are ignored."""
        return fingerprint({"name": action.name, "keys": sorted(str(k) for k in action.params)})

    def observe(self, action: Action) -> int:
        """Record one observation of ``action``'s shape; returns the new observation count."""
        key = self.shape(action)
        with self._lock:
            count = self._seen.pop(key, 0) + 1
            self._seen[key] = count
            while len(self._seen) > self.max_tracked:
                self._seen.popitem(last=False)
            return count

    def observations(self, action: Action) -> int:
        with self._lock:
            return self._seen.get(self.shape(action), 0)

    def forget(self, action: Action) -> bool:
        with self._lock:
            return self._seen.pop(self.shape(action), None) is not None

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()

    def __len__(self) -> int:
        return len(self._seen)

    def evaluate(self, action: Action, context: Mapping[str, Any]) -> SignalResult:
        key = self.shape(action)
        with self._lock:
            count = self._seen.get(key, 0)
            if count:
                self._seen.move_to_end(key)
        if count >= self.min_observations:
            return SignalResult(self.name, 0.0, f"action {action.name!r} seen {count} times")
        return SignalResult(
            self.name,
            1.0,
            f"novel action {action.name!r} (seen {count} of {self.min_observations} required)",
        )


class DisagreementSignal:
    """Scores ``1 - share of the most common candidate`` among alternatives in ``context[key]``.

    Candidates are compared by canonical fingerprint. Missing key or fewer than two candidates score 0;
    a non-list value raises :class:`ValidationError` (which the detector turns into a forced review).
    """

    def __init__(self, key: str = "candidates", *, name: str = "disagreement") -> None:
        self.name = validate_name(name, "signal name")
        if not isinstance(key, str) or not key:
            raise ValidationError("disagreement key must be a non-empty string")
        self.key = key

    def evaluate(self, action: Action, context: Mapping[str, Any]) -> SignalResult:
        candidates = context.get(self.key)
        if candidates is None:
            return SignalResult(self.name, 0.0, "")
        if not isinstance(candidates, (list, tuple)):
            raise ValidationError(f"context[{self.key!r}] must be a list, got {type(candidates).__name__}")
        total = len(candidates)
        if total < 2:
            return SignalResult(self.name, 0.0, "")
        top = Counter(fingerprint(c) for c in candidates).most_common(1)[0][1]
        score = 1.0 - top / total
        reason = f"candidates disagree: top answer has {top}/{total} agreement" if score > 0 else ""
        return SignalResult(self.name, score, reason)


# -------------------------------------------------------------------------------------------- detector


@dataclass(frozen=True)
class Assessment:
    """The immutable, explainable outcome of assessing one action."""

    id: str
    action_name: str
    action_fingerprint: str
    risk: RiskLevel
    score: float
    threshold: float
    requires_review: bool
    reasons: tuple[str, ...]
    signal_results: tuple[SignalResult, ...]
    created_at: float
    aggregation: str = "max"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action_name": self.action_name,
            "action_fingerprint": self.action_fingerprint,
            "risk": self.risk.name,
            "score": self.score,
            "threshold": self.threshold,
            "requires_review": self.requires_review,
            "reasons": list(self.reasons),
            "signal_results": [r.to_dict() for r in self.signal_results],
            "created_at": self.created_at,
            "aggregation": self.aggregation,
        }


AssessItem = Action | tuple[Action, Mapping[str, Any] | None]


class UncertaintyDetector:
    """Runs signals over an action, aggregates their scores and decides whether a human must review it.

    Review is required when the aggregated score is ``>= threshold`` or any signal forces review.
    Weights (default 1.0) scale each signal's score: ``max`` and ``noisy_or`` cap ``weight * score`` at 1,
    ``weighted`` computes ``sum(w*s) / sum(w)``.
    """

    def __init__(
        self,
        signals: Iterable[Signal] = (),
        *,
        threshold: float = 0.5,
        aggregation: Aggregation | str = "max",
        weights: Mapping[str, float] | None = None,
        clock: Clock | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        if aggregation not in AGGREGATIONS:
            expected = ", ".join(sorted(AGGREGATIONS))
            raise ValidationError(f"unknown aggregation {aggregation!r}; expected one of {expected}")
        self.threshold = _unit_interval(threshold, "detector threshold")
        self.aggregation: str = aggregation
        self._weights: dict[str, float] = {}
        for name, weight in (weights or {}).items():
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight):
                raise ValidationError(f"weight for {name!r} must be a finite number")
            if weight < 0:
                raise ValidationError(f"weight for {name!r} must be non-negative")
            self._weights[name] = float(weight)
        self.clock: Clock = clock or SystemClock()
        self.event_sink: EventSink = event_sink or null_sink
        self._signals: tuple[Signal, ...] = ()
        for signal in signals:
            self.add_signal(signal)

    # ------------------------------------------------------------------ registry (copy-on-write)

    @property
    def signals(self) -> tuple[Signal, ...]:
        return self._signals

    @property
    def weights(self) -> Mapping[str, float]:
        return MappingProxyType(self._weights)

    def add_signal(self, signal: Signal) -> None:
        name = getattr(signal, "name", None)
        validate_name(name, "signal name")  # type: ignore[arg-type]
        if not callable(getattr(signal, "evaluate", None)):
            raise ValidationError(f"signal {name!r} has no evaluate() method")
        if any(s.name == name for s in self._signals):
            raise ValidationError(f"duplicate signal name {name!r}")
        self._signals = (*self._signals, signal)

    def remove_signal(self, name: str) -> Signal:
        for signal in self._signals:
            if signal.name == name:
                self._signals = tuple(s for s in self._signals if s is not signal)
                return signal
        raise NotFoundError(f"no signal named {name!r}")

    def get_signal(self, name: str) -> Signal:
        for signal in self._signals:
            if signal.name == name:
                return signal
        raise NotFoundError(f"no signal named {name!r}")

    def observe(self, action: Action) -> None:
        """Forward an observation to every signal that learns (e.g. :class:`NoveltySignal`)."""
        for signal in self._signals:
            observe = getattr(signal, "observe", None)
            if callable(observe):
                observe(action)

    # ------------------------------------------------------------------ assessment

    def assess(self, action: Action, context: Mapping[str, Any] | None = None) -> Assessment:
        if not isinstance(action, Action):
            raise ValidationError(f"expected an Action, got {type(action).__name__}")
        if context is None:
            context = MappingProxyType({})
        elif not isinstance(context, Mapping):
            raise ValidationError(f"context must be a mapping, got {type(context).__name__}")

        results = tuple(self._run(signal, action, context) for signal in self._signals)
        score = self._aggregate(results)
        requires_review = score >= self.threshold or any(r.forces_review for r in results)
        ranked = sorted(
            (r for r in results if r.score > 0 or r.forces_review),
            key=lambda r: (-r.score, not r.forces_review),
        )
        reasons = tuple(r.reason or f"signal {r.name} scored {r.score:.2f}" for r in ranked)
        assessment = Assessment(
            id=new_id("asm"),
            action_name=action.name,
            action_fingerprint=action.fingerprint(),
            risk=action.risk,
            score=score,
            threshold=self.threshold,
            requires_review=requires_review,
            reasons=reasons,
            signal_results=results,
            created_at=self.clock.now(),
            aggregation=self.aggregation,
        )
        self._publish(assessment)
        return assessment

    async def assess_many(self, items: Iterable[AssessItem]) -> list[Assessment]:
        """Assess a batch in order; yields to the event loop between items."""
        out: list[Assessment] = []
        for item in items:
            if isinstance(item, tuple):
                if len(item) != 2:
                    raise ValidationError("batch items must be an Action or an (Action, context) pair")
                action, context = item
            else:
                action, context = item, None
            out.append(self.assess(action, context))
            await asyncio.sleep(0)
        return out

    # ------------------------------------------------------------------ internals

    def _run(self, signal: Signal, action: Action, context: Mapping[str, Any]) -> SignalResult:
        name = signal.name
        try:
            result = signal.evaluate(action, context)
            if not isinstance(result, SignalResult):
                raise TypeError(f"evaluate() returned {type(result).__name__}, not SignalResult")
        except Exception as exc:  # noqa: BLE001 - fail safe: a broken signal must force review
            logger.warning("uncertainty signal %r failed for %s: %r", name, action.name, exc)
            return SignalResult(name, 1.0, f"signal {name} failed: {type(exc).__name__}: {exc}", True)
        if result.name != name:
            result = replace(result, name=name)
        return result

    def _weighted(self, result: SignalResult) -> float:
        return min(1.0, self._weights.get(result.name, 1.0) * result.score)

    def _aggregate(self, results: Sequence[SignalResult]) -> float:
        if not results:
            return 0.0
        if self.aggregation == "max":
            return max(self._weighted(r) for r in results)
        if self.aggregation == "noisy_or":
            remaining = 1.0
            for r in results:
                remaining *= 1.0 - self._weighted(r)
            return min(1.0, max(0.0, 1.0 - remaining))
        total = sum(self._weights.get(r.name, 1.0) for r in results)
        if total <= 0:
            return 0.0
        mean = sum(self._weights.get(r.name, 1.0) * r.score for r in results) / total
        return min(1.0, max(0.0, mean))

    def _publish(self, assessment: Assessment) -> None:
        data = {
            "action": assessment.action_name,
            "action_fingerprint": assessment.action_fingerprint,
            "score": assessment.score,
            "threshold": assessment.threshold,
            "requires_review": assessment.requires_review,
            "reasons": list(assessment.reasons),
        }
        kinds = [EVENT_ASSESSED]
        if assessment.requires_review:
            kinds.append(EVENT_REVIEW_REQUIRED)
        for kind in kinds:
            event = Event(kind, subject_id=assessment.id, data=dict(data), timestamp=assessment.created_at)
            try:
                self.event_sink(event)
            except Exception:  # noqa: BLE001 - auditing problems must not break the decision
                logger.exception("event sink failed for %s", kind)


def default_detector(*, extra_signals: Iterable[Signal] = (), **kwargs: Any) -> UncertaintyDetector:
    """A conservative detector: confidence (floor 0.5), risk (review at HIGH) and novelty signals.

    ``kwargs`` go to :class:`UncertaintyDetector` (threshold, aggregation, weights, clock, event_sink).
    """
    signals: list[Signal] = [ConfidenceSignal(floor=0.5), RiskSignal(RiskLevel.HIGH), NoveltySignal()]
    signals.extend(extra_signals)
    return UncertaintyDetector(signals, **kwargs)
