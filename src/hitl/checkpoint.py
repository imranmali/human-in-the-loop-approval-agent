"""Pause & resume: persist an agent run's state at a checkpoint and resume it once with validated human input.

Overview
--------
* :class:`FieldSpec` / :class:`InputSchema` declare and validate the input a human must supply on resume.
* :class:`PauseResumeManager` pauses a run (creating a :class:`Checkpoint`), and resumes it exactly once,
  returning a :class:`ResumedContext` whose ``merged`` value is ``{**state, **human_input}`` (the human's
  cleaned input overlays the saved state at the top level; on key collisions the human wins).
* :class:`CheckpointStore` is the pluggable async persistence Protocol;
  :class:`InMemoryCheckpointStore` is the default implementation.

Guarantees on resume: not-found → ``NotFoundError``; not ACTIVE → ``StateError`` (single use / replay
protection); past expiry → ``ExpiredError`` (and the checkpoint is marked EXPIRED); stored state no longer
matching its fingerprint (or a caller-supplied expected fingerprint) → ``IntegrityError``; invalid input →
:class:`SchemaValidationError` with the checkpoint left ACTIVE so the human can retry.

Events (never containing raw human input values): ``checkpoint.paused``, ``checkpoint.resumed``,
``checkpoint.resume_rejected``, ``checkpoint.discarded``, ``checkpoint.expired``.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import enum
import hmac
import logging
import math
import re
import weakref
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

from hitl.core import (
    Clock,
    Event,
    EventSink,
    ExpiredError,
    IntegrityError,
    NotFoundError,
    StateError,
    SystemClock,
    ValidationError,
    fingerprint,
    new_id,
    null_sink,
)

__all__ = [
    "MISSING",
    "Checkpoint",
    "CheckpointPage",
    "CheckpointStatus",
    "CheckpointStore",
    "FieldSpec",
    "InMemoryCheckpointStore",
    "InputSchema",
    "PauseResumeManager",
    "ResumedContext",
    "SchemaValidationError",
]

logger = logging.getLogger("hitl.checkpoint")

MAX_PAGE_SIZE: Final = 1000


class _Missing:
    """Sentinel type marking "no default" on a :class:`FieldSpec`."""

    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False

    def __copy__(self) -> _Missing:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> _Missing:
        return self


MISSING: Final = _Missing()


class SchemaValidationError(ValidationError):
    """Human input failed schema validation; ``errors`` lists every problem found."""

    def __init__(self, errors: list[str]) -> None:
        self.errors: list[str] = list(errors)
        super().__init__("invalid input: " + "; ".join(self.errors))


# --------------------------------------------------------------------------------------------- JSON helpers


def _to_json(value: Any, path: str) -> Any:
    """Return a fresh deep copy of ``value`` made of plain JSON types, or raise ValidationError.

    Accepts dict (str keys), list/tuple (→ list), str, bool, None and finite int/float.
    """
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError(f"{path}: non-finite number is not JSON-serializable")
        return float(value)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValidationError(f"{path}: key {key!r} is not a string")
            out[key] = _to_json(item, f"{path}.{key}")
        return out
    if isinstance(value, (list, tuple)):
        return [_to_json(item, f"{path}[{i}]") for i, item in enumerate(value)]
    raise ValidationError(f"{path}: value of type {type(value).__name__} is not JSON-serializable")


def _require_text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{what} must be a non-empty string")
    return value


# ------------------------------------------------------------------------------------------------- schema


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _type_names(types: tuple[type, ...]) -> list[str]:
    return [t.__name__ for t in types]


@dataclass(frozen=True)
class FieldSpec:
    """Declaration of one input field.

    ``type`` is a type or tuple of types (``bool`` never satisfies ``int``/``float`` unless declared; ``int``
    satisfies ``float``; ``None`` only matches when ``type(None)`` is declared). ``min``/``max`` bound numbers
    by value and ``str``/``list``/``tuple``/``dict`` by length. ``pattern`` must fully match ``str`` values.
    ``validator(value)`` returning ``False`` or a ``str`` message marks the value invalid. A field with a
    ``default`` is never reported missing; a field with ``required=False`` and no default is simply omitted.
    """

    type: type | tuple[type, ...]
    required: bool = True
    default: Any = MISSING
    choices: Collection[Any] | None = None
    min: float | None = None
    max: float | None = None
    pattern: str | None = None
    validator: Callable[[Any], bool | str | None] | None = None
    description: str = ""
    _types: tuple[type, ...] = field(init=False, repr=False, compare=False)
    _regex: re.Pattern[str] | None = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        types = self.type if isinstance(self.type, tuple) else (self.type,)
        if not types or not all(isinstance(t, type) for t in types):
            raise ValidationError("FieldSpec.type must be a type or a non-empty tuple of types")
        object.__setattr__(self, "_types", types)
        for bound_name in ("min", "max"):
            bound = getattr(self, bound_name)
            if bound is not None and not _is_number(bound):
                raise ValidationError(f"FieldSpec.{bound_name} must be a number")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValidationError("FieldSpec.min must not exceed FieldSpec.max")
        compiled = None
        if self.pattern is not None:
            if not isinstance(self.pattern, str):
                raise ValidationError("FieldSpec.pattern must be a string")
            try:
                compiled = re.compile(self.pattern)
            except re.error as exc:
                raise ValidationError(f"FieldSpec.pattern is not a valid regular expression: {exc}") from None
        object.__setattr__(self, "_regex", compiled)
        if self.choices is not None:
            if isinstance(self.choices, (str, bytes)) or not isinstance(self.choices, Collection):
                raise ValidationError("FieldSpec.choices must be a non-string collection")
            choices = self.choices
            if isinstance(choices, (set, frozenset)):
                choices = sorted(choices, key=repr)  # deterministic order for to_dict()
            object.__setattr__(self, "choices", tuple(choices))
        if self.validator is not None and not callable(self.validator):
            raise ValidationError("FieldSpec.validator must be callable")
        if not isinstance(self.description, str):
            raise ValidationError("FieldSpec.description must be a string")
        if self.default is not MISSING:
            problems = self.check(self.default, "default")
            if problems:
                raise ValidationError("FieldSpec.default is invalid: " + "; ".join(problems))

    @property
    def types(self) -> tuple[type, ...]:
        return self._types

    def _type_ok(self, value: Any) -> bool:
        types = self.types
        if object in types:
            return True
        if isinstance(value, bool):
            return bool in types
        if isinstance(value, int) and float in types:
            return True
        return isinstance(value, types)

    def check(self, value: Any, name: str) -> list[str]:
        """Return the list of problems with ``value`` (empty when valid). Messages never include the value."""
        if not self._type_ok(value):
            expected = " | ".join(_type_names(self.types))
            return [f"{name}: expected {expected}, got {type(value).__name__}"]
        problems: list[str] = []
        if self.choices is not None and not _in_choices(value, self.choices):
            problems.append(f"{name}: must be one of {list(self.choices)!r}")
        measured: float | None = None
        label = ""
        if _is_number(value):
            measured, label = value, ""
        elif isinstance(value, (str, list, tuple, dict)):
            measured, label = len(value), "length "
        if measured is not None:
            if self.min is not None and measured < self.min:
                problems.append(f"{name}: {label}must be >= {self.min}")
            if self.max is not None and measured > self.max:
                problems.append(f"{name}: {label}must be <= {self.max}")
        regex = self._regex
        if regex is not None and isinstance(value, str) and regex.fullmatch(value) is None:
            problems.append(f"{name}: does not match pattern {self.pattern!r}")
        if self.validator is not None:
            try:
                verdict = self.validator(value)
            except Exception as exc:  # noqa: BLE001 - a crashing validator means "invalid"
                problems.append(f"{name}: validator raised {type(exc).__name__}")
            else:
                if verdict is False:
                    problems.append(f"{name}: rejected by validator")
                elif isinstance(verdict, str):
                    problems.append(f"{name}: {verdict}")
        return problems

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable description of this field for a human-facing UI."""
        out: dict[str, Any] = {
            "type": _type_names(self.types),
            "required": self.required and self.default is MISSING,
            "description": self.description,
            "has_validator": self.validator is not None,
        }
        if self.default is not MISSING:
            out["default"] = _describe(self.default)
        if self.choices is not None:
            out["choices"] = [_describe(c) for c in self.choices]
        if self.min is not None:
            out["min"] = self.min
        if self.max is not None:
            out["max"] = self.max
        if self.pattern is not None:
            out["pattern"] = self.pattern
        return out


def _in_choices(value: Any, choices: tuple[Any, ...]) -> bool:
    # Avoid True == 1 style matches between bools and numbers.
    return any(value == c and isinstance(value, bool) == isinstance(c, bool) for c in choices)


def _describe(value: Any) -> Any:
    try:
        return _to_json(value, "value")
    except ValidationError:
        return repr(value)


class InputSchema:
    """Validates a human input mapping against named :class:`FieldSpec` declarations.

    ``validate`` returns a cleaned deep copy (defaults applied, undeclared keys passed through only when
    ``allow_extra``) or raises :class:`SchemaValidationError` listing every problem.
    """

    __slots__ = ("_fields", "allow_extra")

    def __init__(self, fields: Mapping[str, FieldSpec], allow_extra: bool = False) -> None:
        if not isinstance(fields, Mapping):
            raise ValidationError("InputSchema fields must be a mapping of name -> FieldSpec")
        checked: dict[str, FieldSpec] = {}
        for name, spec in fields.items():
            if not isinstance(name, str) or not name:
                raise ValidationError("InputSchema field names must be non-empty strings")
            if not isinstance(spec, FieldSpec):
                raise ValidationError(f"InputSchema field {name!r} must be a FieldSpec")
            checked[name] = spec
        self._fields = checked
        self.allow_extra = bool(allow_extra)

    @property
    def fields(self) -> Mapping[str, FieldSpec]:
        return dict(self._fields)

    def validate(self, data: Any) -> dict[str, Any]:
        if data is None:
            data = {}
        if not isinstance(data, Mapping):
            raise SchemaValidationError([f"input must be a mapping, got {type(data).__name__}"])
        errors: list[str] = []
        cleaned: dict[str, Any] = {}
        for name, spec in self._fields.items():
            if name in data:
                problems = spec.check(data[name], name)
                if problems:
                    errors.extend(problems)
                else:
                    cleaned[name] = copy.deepcopy(data[name])
            elif spec.default is not MISSING:
                cleaned[name] = copy.deepcopy(spec.default)
            elif spec.required:
                errors.append(f"{name}: required")
        for key in data:
            if key in self._fields:
                continue
            if not isinstance(key, str):
                errors.append(f"input key of type {type(key).__name__} is not a string")
            elif self.allow_extra:
                cleaned[key] = copy.deepcopy(data[key])
            else:
                errors.append(f"{key}: unexpected field")
        if errors:
            raise SchemaValidationError(errors)
        return cleaned

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable description of the expected input, for a human review UI."""
        return {
            "fields": {name: spec.to_dict() for name, spec in self._fields.items()},
            "allow_extra": self.allow_extra,
        }

    def __repr__(self) -> str:
        return f"InputSchema(fields={list(self._fields)!r}, allow_extra={self.allow_extra})"

    def __copy__(self) -> InputSchema:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> InputSchema:
        return self  # immutable once constructed


# ---------------------------------------------------------------------------------------------- checkpoint


class CheckpointStatus(enum.StrEnum):
    ACTIVE = "active"
    RESUMED = "resumed"
    DISCARDED = "discarded"
    EXPIRED = "expired"


@dataclass
class Checkpoint:
    """A paused run's saved state. ``version`` increments on every status transition."""

    id: str
    run_id: str
    step: str
    state: dict[str, Any]
    state_fingerprint: str
    created_at: float
    input_schema: InputSchema | None = None
    expires_at: float | None = None
    status: CheckpointStatus = CheckpointStatus.ACTIVE
    resumed_at: float | None = None
    resumed_by: str | None = None
    discarded_by: str | None = None
    discard_reason: str | None = None
    closed_at: float | None = None
    version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.status is CheckpointStatus.ACTIVE

    def is_overdue(self, now: float) -> bool:
        """True when the checkpoint has an expiry and ``now`` is at or after it."""
        return self.expires_at is not None and now >= self.expires_at

    def copy(self) -> Checkpoint:
        """Independent copy (state and metadata deep-copied; the immutable schema is shared)."""
        return dataclasses.replace(
            self, state=copy.deepcopy(self.state), metadata=copy.deepcopy(self.metadata)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "step": self.step,
            "state": copy.deepcopy(self.state),
            "state_fingerprint": self.state_fingerprint,
            "input_schema": self.input_schema.to_dict() if self.input_schema is not None else None,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status.value,
            "resumed_at": self.resumed_at,
            "resumed_by": self.resumed_by,
            "discarded_by": self.discarded_by,
            "discard_reason": self.discard_reason,
            "closed_at": self.closed_at,
            "version": self.version,
            "metadata": copy.deepcopy(self.metadata),
        }


@dataclass(frozen=True)
class CheckpointPage:
    """One page of a listing; pass ``next_cursor`` back to continue (None when exhausted)."""

    items: list[Checkpoint]
    next_cursor: str | None = None


@dataclass(frozen=True)
class ResumedContext:
    """What a resumed run continues with. ``merged == {**state, **human_input}`` (human input wins)."""

    checkpoint_id: str
    run_id: str
    step: str
    state: dict[str, Any]
    human_input: dict[str, Any]
    merged: dict[str, Any]
    resumed_by: str
    resumed_at: float
    state_fingerprint: str
    input_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "run_id": self.run_id,
            "step": self.step,
            "state": copy.deepcopy(self.state),
            "human_input": copy.deepcopy(self.human_input),
            "merged": copy.deepcopy(self.merged),
            "resumed_by": self.resumed_by,
            "resumed_at": self.resumed_at,
            "state_fingerprint": self.state_fingerprint,
            "input_fingerprint": self.input_fingerprint,
        }


# --------------------------------------------------------------------------------------------------- store


@runtime_checkable
class CheckpointStore(Protocol):
    """Async persistence for checkpoints.

    ``put(cp, expected_version=None)`` creates ``cp`` (StateError if the id exists); with ``expected_version``
    it replaces the stored checkpoint only if its version equals ``expected_version`` (StateError otherwise,
    NotFoundError if absent). Implementations must return copies that callers cannot use to mutate storage.
    """

    async def get(self, checkpoint_id: str) -> Checkpoint | None: ...

    async def put(self, checkpoint: Checkpoint, expected_version: int | None = None) -> None: ...

    async def list_by_run(self, run_id: str) -> list[Checkpoint]: ...

    async def list_active(self, limit: int = 100, cursor: str | None = None) -> CheckpointPage: ...


class InMemoryCheckpointStore:
    """Process-local :class:`CheckpointStore` (default).

    Stores and returns copies, so mutating a checkpoint you hold never changes what is stored. The backing
    dict ``_items`` (id -> Checkpoint) is intentionally reachable: tests simulate tampering at rest by
    mutating it directly, e.g. ``store._items[cid].state["amount"] = 10**6``. No tamper API exists in
    production code.
    """

    def __init__(self) -> None:
        self._items: dict[str, Checkpoint] = {}
        self._seq: dict[str, int] = {}
        self._next_seq = 0
        self._lock = asyncio.Lock()

    async def get(self, checkpoint_id: str) -> Checkpoint | None:
        cp = self._items.get(checkpoint_id)
        return cp.copy() if cp is not None else None

    async def put(self, checkpoint: Checkpoint, expected_version: int | None = None) -> None:
        async with self._lock:
            current = self._items.get(checkpoint.id)
            if expected_version is None:
                if current is not None:
                    raise StateError(f"checkpoint {checkpoint.id!r} already exists")
                self._next_seq += 1
                self._seq[checkpoint.id] = self._next_seq
            else:
                if current is None:
                    raise NotFoundError(f"checkpoint {checkpoint.id!r} not found")
                if current.version != expected_version:
                    raise StateError(
                        f"checkpoint {checkpoint.id!r} version conflict: "
                        f"expected {expected_version}, stored {current.version}"
                    )
            self._items[checkpoint.id] = checkpoint.copy()

    async def list_by_run(self, run_id: str) -> list[Checkpoint]:
        ids = sorted(
            (cid for cid, cp in self._items.items() if cp.run_id == run_id), key=self._seq.__getitem__
        )
        return [self._items[cid].copy() for cid in ids]

    async def list_active(self, limit: int = 100, cursor: str | None = None) -> CheckpointPage:
        limit = _check_limit(limit)
        after = _parse_cursor(cursor)
        ordered = sorted(
            (
                (seq, cid)
                for cid, seq in self._seq.items()
                if seq > after and self._items[cid].status is CheckpointStatus.ACTIVE
            ),
        )
        page = ordered[:limit]
        next_cursor = str(page[-1][0]) if len(ordered) > limit else None
        return CheckpointPage(items=[self._items[cid].copy() for _, cid in page], next_cursor=next_cursor)

    def __len__(self) -> int:
        return len(self._items)


def _check_limit(limit: Any) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValidationError(f"limit must be an integer between 1 and {MAX_PAGE_SIZE}")
    return limit


def _parse_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor.isdigit():
        raise ValidationError(f"invalid cursor {cursor!r}")
    return int(cursor)


# ------------------------------------------------------------------------------------------------- manager


class PauseResumeManager:
    """Pauses agent runs at checkpoints and resumes each exactly once with validated human input.

    Concurrency: a per-checkpoint ``asyncio.Lock`` serializes transitions inside this manager; the store's
    optimistic versioning protects managers that share a store.
    """

    def __init__(
        self,
        store: CheckpointStore | None = None,
        clock: Clock | None = None,
        event_sink: EventSink | None = None,
        default_ttl: float | None = None,
    ) -> None:
        self.store: CheckpointStore = store if store is not None else InMemoryCheckpointStore()
        self.clock: Clock = clock if clock is not None else SystemClock()
        self._sink: EventSink = event_sink if event_sink is not None else null_sink
        self.default_ttl = _check_ttl(default_ttl, "default_ttl")
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    # -- helpers -------------------------------------------------------------------------------------------

    def _lock_for(self, checkpoint_id: str) -> asyncio.Lock:
        lock = self._locks.get(checkpoint_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[checkpoint_id] = lock
        return lock

    def _emit(self, kind: str, cp: Checkpoint, actor: str = "system", **data: Any) -> None:
        event = Event(
            kind=kind,
            subject_id=cp.id,
            actor=actor,
            data={"run_id": cp.run_id, "step": cp.step, **data},
            timestamp=self.clock.now(),
        )
        try:
            self._sink(event)
        except Exception:  # noqa: BLE001 - a broken sink must not break pause/resume
            logger.exception("event sink failed for %s", kind)

    async def _load(self, checkpoint_id: str) -> Checkpoint:
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise NotFoundError(f"checkpoint {checkpoint_id!r} not found")
        cp = await self.store.get(checkpoint_id)
        if cp is None:
            raise NotFoundError(f"checkpoint {checkpoint_id!r} not found")
        return cp

    def _reject(self, cp: Checkpoint, actor: str, reason: str, errors: list[str]) -> None:
        self._emit("checkpoint.resume_rejected", cp, actor, reason=reason, errors=list(errors))

    async def _mark_expired(self, cp: Checkpoint, now: float) -> Checkpoint:
        updated = dataclasses.replace(
            cp, status=CheckpointStatus.EXPIRED, closed_at=now, version=cp.version + 1
        )
        await self.store.put(updated, expected_version=cp.version)
        self._emit("checkpoint.expired", updated, expires_at=cp.expires_at)
        return updated

    # -- public API ----------------------------------------------------------------------------------------

    async def pause(
        self,
        run_id: str,
        step: str,
        state: Mapping[str, Any],
        *,
        input_schema: InputSchema | None = None,
        ttl: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Checkpoint:
        """Persist ``state`` for ``run_id`` at ``step`` and return the new ACTIVE checkpoint."""
        _require_text(run_id, "run_id")
        _require_text(step, "step")
        if not isinstance(state, Mapping):
            raise ValidationError(f"state must be a mapping, got {type(state).__name__}")
        if input_schema is not None and not isinstance(input_schema, InputSchema):
            raise ValidationError("input_schema must be an InputSchema")
        ttl = _check_ttl(ttl, "ttl")
        clean_state = _to_json(state, "state")
        clean_meta = _to_json(dict(metadata or {}), "metadata")
        effective_ttl = ttl if ttl is not None else self.default_ttl
        now = self.clock.now()
        cp = Checkpoint(
            id=new_id("ckpt"),
            run_id=run_id,
            step=step,
            state=clean_state,
            state_fingerprint=fingerprint(clean_state),
            created_at=now,
            input_schema=input_schema,
            expires_at=now + effective_ttl if effective_ttl is not None else None,
            metadata=clean_meta,
        )
        await self.store.put(cp)
        self._emit(
            "checkpoint.paused",
            cp,
            state_fingerprint=cp.state_fingerprint,
            expires_at=cp.expires_at,
            input_fields=list(input_schema.fields) if input_schema is not None else [],
        )
        return cp.copy()

    async def resume(
        self,
        checkpoint_id: str,
        human_input: Mapping[str, Any] | None = None,
        *,
        actor: str,
        expected_fingerprint: str | None = None,
    ) -> ResumedContext:
        """Resume an ACTIVE checkpoint once with validated ``human_input``; see module docs for errors."""
        _require_text(actor, "actor")
        async with self._lock_for(checkpoint_id):
            cp = await self._load(checkpoint_id)
            now = self.clock.now()

            if cp.status is CheckpointStatus.EXPIRED:
                self._reject(cp, actor, "expired", ["checkpoint has expired"])
                raise ExpiredError(f"checkpoint {cp.id!r} has expired")
            if cp.status is not CheckpointStatus.ACTIVE:
                self._reject(cp, actor, "state", [f"checkpoint is {cp.status.value}"])
                raise StateError(f"checkpoint {cp.id!r} is {cp.status.value}, not active")
            if cp.is_overdue(now):
                try:
                    await self._mark_expired(cp, now)
                finally:
                    self._reject(cp, actor, "expired", ["checkpoint has expired"])
                raise ExpiredError(f"checkpoint {cp.id!r} expired at {cp.expires_at}")

            actual = fingerprint(cp.state)
            if not hmac.compare_digest(actual, cp.state_fingerprint):
                self._reject(cp, actor, "integrity", ["stored state does not match its fingerprint"])
                raise IntegrityError(f"checkpoint {cp.id!r} state does not match its fingerprint")
            if expected_fingerprint is not None and not hmac.compare_digest(
                str(expected_fingerprint), cp.state_fingerprint
            ):
                self._reject(cp, actor, "integrity", ["state fingerprint does not match the expected value"])
                raise IntegrityError(
                    f"checkpoint {cp.id!r} state fingerprint does not match the expected value"
                )

            try:
                cleaned = self._clean_input(cp, human_input)
            except SchemaValidationError as exc:
                self._reject(cp, actor, "validation", exc.errors)
                raise

            resumed = dataclasses.replace(
                cp,
                status=CheckpointStatus.RESUMED,
                resumed_at=now,
                resumed_by=actor,
                closed_at=now,
                version=cp.version + 1,
            )
            try:
                await self.store.put(resumed, expected_version=cp.version)
            except StateError as exc:
                self._reject(cp, actor, "state", [str(exc)])
                raise

        input_fp = fingerprint(cleaned)
        self._emit(
            "checkpoint.resumed",
            resumed,
            actor,
            state_fingerprint=cp.state_fingerprint,
            input_fingerprint=input_fp,
            input_keys=sorted(cleaned),
        )
        return ResumedContext(
            checkpoint_id=cp.id,
            run_id=cp.run_id,
            step=cp.step,
            state=copy.deepcopy(cp.state),
            human_input=copy.deepcopy(cleaned),
            merged={**copy.deepcopy(cp.state), **copy.deepcopy(cleaned)},
            resumed_by=actor,
            resumed_at=now,
            state_fingerprint=cp.state_fingerprint,
            input_fingerprint=input_fp,
        )

    @staticmethod
    def _clean_input(cp: Checkpoint, human_input: Any) -> dict[str, Any]:
        if cp.input_schema is not None:
            cleaned: Any = cp.input_schema.validate(human_input)
        else:
            if human_input is None:
                human_input = {}
            if not isinstance(human_input, Mapping):
                raise SchemaValidationError([f"input must be a mapping, got {type(human_input).__name__}"])
            cleaned = human_input
        try:
            result: dict[str, Any] = _to_json(cleaned, "input")
        except ValidationError as exc:
            raise SchemaValidationError([str(exc)]) from None
        return result

    async def discard(self, checkpoint_id: str, actor: str, reason: str = "") -> Checkpoint:
        """Mark an ACTIVE checkpoint DISCARDED; StateError if it is not ACTIVE."""
        _require_text(actor, "actor")
        if not isinstance(reason, str):
            raise ValidationError("reason must be a string")
        async with self._lock_for(checkpoint_id):
            cp = await self._load(checkpoint_id)
            if cp.status is not CheckpointStatus.ACTIVE:
                raise StateError(f"checkpoint {cp.id!r} is {cp.status.value}, not active")
            now = self.clock.now()
            updated = dataclasses.replace(
                cp,
                status=CheckpointStatus.DISCARDED,
                discarded_by=actor,
                discard_reason=reason,
                closed_at=now,
                version=cp.version + 1,
            )
            await self.store.put(updated, expected_version=cp.version)
        self._emit("checkpoint.discarded", updated, actor, reason=reason)
        return updated.copy()

    async def get(self, checkpoint_id: str) -> Checkpoint:
        """Return the checkpoint or raise NotFoundError."""
        return await self._load(checkpoint_id)

    async def expire_due(self, *, page_size: int = 500) -> list[Checkpoint]:
        """Mark every ACTIVE checkpoint past its expiry as EXPIRED and return them."""
        now = self.clock.now()
        due: list[str] = []
        cursor: str | None = None
        while True:
            page = await self.store.list_active(limit=page_size, cursor=cursor)
            due.extend(cp.id for cp in page.items if cp.is_overdue(now))
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        expired: list[Checkpoint] = []
        for cid in due:
            async with self._lock_for(cid):
                cp = await self.store.get(cid)
                if cp is None or not cp.is_active or not cp.is_overdue(now):
                    continue
                try:
                    expired.append(await self._mark_expired(cp, now))
                except StateError:
                    continue  # another manager transitioned it first
        return expired

    async def active_for_run(self, run_id: str) -> list[Checkpoint]:
        """Resumable checkpoints of ``run_id``: ACTIVE and not past expiry, in creation order."""
        now = self.clock.now()
        return [cp for cp in await self.store.list_by_run(run_id) if cp.is_active and not cp.is_overdue(now)]


def _check_ttl(ttl: Any, what: str) -> float | None:
    if ttl is None:
        return None
    if not _is_number(ttl) or not math.isfinite(ttl) or ttl <= 0:
        raise ValidationError(f"{what} must be a positive number of seconds")
    return float(ttl)
