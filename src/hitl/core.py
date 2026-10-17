"""Core data model, error hierarchy and event plumbing shared by every hitl module."""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("hitl")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]*$")


class HITLError(Exception):
    """Base class for all hitl errors."""


class ValidationError(HITLError, ValueError):
    """An input, schema or descriptor is malformed or fails validation."""


class NotFoundError(HITLError, LookupError):
    """A referenced request, checkpoint or record does not exist."""


class StateError(HITLError):
    """An operation is not allowed in the object's current state."""


class AuthorizationError(HITLError):
    """The actor is not allowed to perform the operation."""


class IntegrityError(HITLError):
    """Stored data was tampered with or does not match its fingerprint."""


class ExpiredError(StateError):
    """The request or checkpoint has passed its deadline."""


@runtime_checkable
class Clock(Protocol):
    def now(self) -> float: ...


class SystemClock:
    """Wall-clock time in seconds since the epoch."""

    def now(self) -> float:
        return time.time()


class ManualClock:
    """A deterministic clock for tests and simulations."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValidationError("cannot move a clock backwards")
        self._now += seconds
        return self._now


def new_id(prefix: str) -> str:
    """Return a globally unique, prefixed identifier such as ``apr_3f2a...``."""
    if not prefix or not _NAME_RE.match(prefix):
        raise ValidationError(f"invalid id prefix {prefix!r}")
    return f"{prefix}_{uuid.uuid4().hex}"


def validate_name(value: str, what: str = "name") -> str:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise ValidationError(f"invalid {what} {value!r}: use lowercase letters, digits and . _ : -")
    return value


def canonical_json(value: Any) -> str:
    """Deterministic JSON encoding (sorted keys, no whitespace) used for hashing."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default, ensure_ascii=False)


def _json_default(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return repr(value)


def fingerprint(value: Any) -> str:
    """SHA-256 hex digest of the canonical JSON encoding of ``value``."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class RiskLevel(enum.IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


@dataclass(frozen=True)
class Action:
    """An operation the agent proposes to perform, possibly pending human review.

    ``confidence`` is the agent's self-reported confidence in [0, 1]; ``risk`` is
    the declared impact if the action is wrong.
    """

    name: str
    params: Mapping[str, Any] = field(default_factory=dict)
    risk: RiskLevel = RiskLevel.LOW
    confidence: float = 1.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_name(self.name, "action name")
        if not isinstance(self.confidence, (int, float)) or not 0.0 <= float(self.confidence) <= 1.0:
            raise ValidationError(f"action {self.name!r}: confidence must be within [0, 1]")
        if not isinstance(self.risk, RiskLevel):
            object.__setattr__(self, "risk", RiskLevel(self.risk))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "params": dict(self.params),
            "risk": self.risk.name,
            "confidence": float(self.confidence),
            "metadata": dict(self.metadata),
        }

    def fingerprint(self) -> str:
        return fingerprint(self.to_dict())


@dataclass(frozen=True)
class Event:
    """A domain event published by a module; the audit trail records these."""

    kind: str
    subject_id: str
    actor: str = "system"
    data: Mapping[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        validate_name(self.kind, "event kind")
        if not self.subject_id:
            raise ValidationError("event subject_id must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "subject_id": self.subject_id,
            "actor": self.actor,
            "data": dict(self.data),
            "timestamp": self.timestamp,
        }


EventSink = Callable[[Event], None]


def null_sink(event: Event) -> None:
    """An EventSink that discards everything."""


class EventBus:
    """Fan-out of events to many sinks; a failing sink never breaks publishers."""

    def __init__(self) -> None:
        self._sinks: list[EventSink] = []

    def subscribe(self, sink: EventSink) -> Callable[[], None]:
        self._sinks.append(sink)

        def unsubscribe() -> None:
            if sink in self._sinks:
                self._sinks.remove(sink)

        return unsubscribe

    def emit(self, event: Event) -> None:
        for sink in list(self._sinks):
            try:
                sink(event)
            except Exception:  # noqa: BLE001 - isolation is the point
                logger.exception("event sink %r failed for %s", sink, event.kind)

    __call__ = emit
