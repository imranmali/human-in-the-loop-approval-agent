"""Approval workflow: request, authorize, collect and resolve human decisions on proposed actions.

An :class:`ApprovalService` turns a proposed :class:`~hitl.core.Action` into an :class:`ApprovalRequest`
governed by an :class:`ApprovalPolicy` (quorum, eligible roles, timeout, one-step escalation, self-approval
and rejection comment rules). Humans answer with :class:`Verdict` values; a single rejection vetoes the
request while approvals (plain or with modifications) count toward the quorum. Requests that pass their
deadline escalate once (when the policy says so) and otherwise expire, either lazily when someone acts on
them or through :meth:`ApprovalService.expire_due`.

Persistence and approver lookup are pluggable through the :class:`ApprovalStore` and
:class:`ApproverDirectory` protocols; every state change is published as an ``approval.*``
:class:`~hitl.core.Event` to the injected sink.
"""

from __future__ import annotations

import asyncio
import bisect
import copy
import enum
import logging
import weakref
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from hitl.core import (
    Action,
    AuthorizationError,
    Clock,
    Event,
    EventSink,
    ExpiredError,
    IntegrityError,
    NotFoundError,
    RiskLevel,
    StateError,
    SystemClock,
    ValidationError,
    fingerprint,
    new_id,
    null_sink,
)

logger = logging.getLogger("hitl.approvals")

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 1000
SYSTEM_ACTOR = "system"


class ApprovalStatus(enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self is not ApprovalStatus.PENDING


class Verdict(enum.Enum):
    APPROVE = "approve"
    REJECT = "reject"
    MODIFY = "modify"  # approve, with parameter modifications


def _coerce_verdict(value: Verdict | str) -> Verdict:
    if isinstance(value, Verdict):
        return value
    try:
        return Verdict(str(value).lower())
    except ValueError:
        raise ValidationError(f"unknown verdict {value!r}") from None


def _roles(value: Iterable[str] | str | None, what: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        value = (value,)
    roles = frozenset(value)
    for role in roles:
        if not isinstance(role, str) or not role:
            raise ValidationError(f"{what} must be non-empty strings, got {role!r}")
    return roles


def _require_text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{what} must be a non-empty string")
    return value


def _copy_mapping(value: Mapping[str, Any] | None, what: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValidationError(f"{what} must be a mapping")
    return copy.deepcopy(dict(value))


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValidationError(f"limit must be an integer within [1, {MAX_PAGE_SIZE}]")
    return limit


# --------------------------------------------------------------------------------------------- data model


@dataclass(frozen=True)
class Decision:
    """One human's answer to an approval request."""

    approver: str
    verdict: Verdict
    comment: str = ""
    modifications: Mapping[str, Any] = field(default_factory=dict)
    decided_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "approver": self.approver,
            "verdict": self.verdict.value,
            "comment": self.comment,
            "modifications": copy.deepcopy(dict(self.modifications)),
            "decided_at": self.decided_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Decision:
        return cls(
            approver=data["approver"],
            verdict=_coerce_verdict(data["verdict"]),
            comment=data.get("comment", ""),
            modifications=copy.deepcopy(dict(data.get("modifications") or {})),
            decided_at=float(data.get("decided_at", 0.0)),
        )


@dataclass(frozen=True)
class Approver:
    """A human allowed to decide approval requests, identified by id and holding roles."""

    id: str
    roles: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _require_text(self.id, "approver id")
        object.__setattr__(self, "roles", _roles(self.roles, "approver roles"))

    def holds_any(self, roles: frozenset[str]) -> bool:
        """True when ``roles`` is empty (anyone qualifies) or the approver holds at least one of them."""
        return not roles or bool(self.roles & roles)


@dataclass(frozen=True)
class ApprovalPolicy:
    """Rules governing who must decide a request, how many approvals it needs and when it times out."""

    required_approvals: int = 1
    required_roles: frozenset[str] = frozenset()
    timeout_seconds: float | None = None
    escalation_roles: frozenset[str] = frozenset()
    allow_self_approval: bool = False
    require_comment_on_reject: bool = True

    def __post_init__(self) -> None:
        quorum = self.required_approvals
        if isinstance(quorum, bool) or not isinstance(quorum, int) or quorum < 1:
            raise ValidationError("required_approvals must be an integer >= 1")
        object.__setattr__(self, "required_roles", _roles(self.required_roles, "required_roles"))
        object.__setattr__(self, "escalation_roles", _roles(self.escalation_roles, "escalation_roles"))
        timeout = self.timeout_seconds
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
                raise ValidationError("timeout_seconds must be a positive number or None")
            object.__setattr__(self, "timeout_seconds", float(timeout))
        if self.escalation_roles and self.timeout_seconds is None:
            raise ValidationError("escalation_roles require timeout_seconds")

    def roles_for_level(self, escalation_level: int) -> frozenset[str]:
        """Roles eligible to decide at the given escalation level (empty means any known approver)."""
        if escalation_level > 0 and self.escalation_roles:
            return self.escalation_roles
        return self.required_roles

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_approvals": self.required_approvals,
            "required_roles": sorted(self.required_roles),
            "timeout_seconds": self.timeout_seconds,
            "escalation_roles": sorted(self.escalation_roles),
            "allow_self_approval": self.allow_self_approval,
            "require_comment_on_reject": self.require_comment_on_reject,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ApprovalPolicy:
        return cls(
            required_approvals=data.get("required_approvals", 1),
            required_roles=frozenset(data.get("required_roles", ())),
            timeout_seconds=data.get("timeout_seconds"),
            escalation_roles=frozenset(data.get("escalation_roles", ())),
            allow_self_approval=bool(data.get("allow_self_approval", False)),
            require_comment_on_reject=bool(data.get("require_comment_on_reject", True)),
        )


def _action_from_dict(data: Mapping[str, Any]) -> Action:
    risk = data.get("risk", RiskLevel.LOW)
    if isinstance(risk, str):
        risk = RiskLevel[risk]
    return Action(
        name=data["name"],
        params=copy.deepcopy(dict(data.get("params") or {})),
        risk=risk,
        confidence=data.get("confidence", 1.0),
        metadata=copy.deepcopy(dict(data.get("metadata") or {})),
    )


def compute_context_fingerprint(action: Action, context: Mapping[str, Any]) -> str:
    """Fingerprint binding the reviewed action and its context together."""
    return fingerprint({"action": action.to_dict(), "context": dict(context)})


@dataclass
class ApprovalRequest:
    """A request for human decisions on a proposed action."""

    id: str
    action: Action
    requester: str
    policy: ApprovalPolicy
    reason: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    context_fingerprint: str = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    decisions: list[Decision] = field(default_factory=list)
    created_at: float = 0.0
    expires_at: float | None = None
    escalation_level: int = 0
    idempotency_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    resolved_at: float | None = None
    resolved_by: str | None = None
    resolution_reason: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    @property
    def approvals_count(self) -> int:
        """Number of decisions counting toward the quorum (APPROVE and MODIFY)."""
        return sum(1 for d in self.decisions if d.verdict in (Verdict.APPROVE, Verdict.MODIFY))

    @property
    def effective_modifications(self) -> dict[str, Any]:
        """MODIFY modifications merged in decision order; later decisions override earlier ones."""
        merged: dict[str, Any] = {}
        for decision in self.decisions:
            if decision.verdict is Verdict.MODIFY:
                merged.update(copy.deepcopy(dict(decision.modifications)))
        return merged

    @property
    def eligible_roles(self) -> frozenset[str]:
        """Roles currently allowed to decide (depends on the escalation level)."""
        return self.policy.roles_for_level(self.escalation_level)

    def effective_action(self) -> Action:
        """The action with :attr:`effective_modifications` applied to its params."""
        params = {**copy.deepcopy(dict(self.action.params)), **self.effective_modifications}
        return Action(
            name=self.action.name,
            params=params,
            risk=self.action.risk,
            confidence=self.action.confidence,
            metadata=dict(self.action.metadata),
        )

    def decided_by(self, approver_id: str) -> bool:
        return any(d.approver == approver_id for d in self.decisions)

    def is_overdue(self, now: float) -> bool:
        return (
            self.status is ApprovalStatus.PENDING and self.expires_at is not None and now >= self.expires_at
        )

    def compute_fingerprint(self) -> str:
        return compute_context_fingerprint(self.action, self.context)

    def verify_integrity(self) -> None:
        """Raise :class:`IntegrityError` when the action or context no longer match the fingerprint."""
        if self.compute_fingerprint() != self.context_fingerprint:
            raise IntegrityError(
                f"approval request {self.id}: action or context does not match its fingerprint"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action.to_dict(),
            "requester": self.requester,
            "reason": self.reason,
            "context": copy.deepcopy(dict(self.context)),
            "context_fingerprint": self.context_fingerprint,
            "policy": self.policy.to_dict(),
            "status": self.status.value,
            "decisions": [d.to_dict() for d in self.decisions],
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "escalation_level": self.escalation_level,
            "idempotency_key": self.idempotency_key,
            "metadata": copy.deepcopy(dict(self.metadata)),
            "resolved_at": self.resolved_at,
            "resolved_by": self.resolved_by,
            "resolution_reason": self.resolution_reason,
            "approvals_count": self.approvals_count,
            "effective_modifications": self.effective_modifications,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ApprovalRequest:
        return cls(
            id=data["id"],
            action=_action_from_dict(data["action"]),
            requester=data["requester"],
            policy=ApprovalPolicy.from_dict(data.get("policy") or {}),
            reason=data.get("reason", ""),
            context=copy.deepcopy(dict(data.get("context") or {})),
            context_fingerprint=data.get("context_fingerprint", ""),
            status=ApprovalStatus(data.get("status", ApprovalStatus.PENDING.value)),
            decisions=[Decision.from_dict(d) for d in data.get("decisions", ())],
            created_at=float(data.get("created_at", 0.0)),
            expires_at=data.get("expires_at"),
            escalation_level=int(data.get("escalation_level", 0)),
            idempotency_key=data.get("idempotency_key"),
            metadata=copy.deepcopy(dict(data.get("metadata") or {})),
            resolved_at=data.get("resolved_at"),
            resolved_by=data.get("resolved_by"),
            resolution_reason=data.get("resolution_reason", ""),
        )


@dataclass(frozen=True)
class Page(Generic[T]):
    """One page of a listing; pass ``next_cursor`` back to get the next page (``None`` means the end)."""

    items: tuple[T, ...]
    next_cursor: str | None = None


# --------------------------------------------------------------------------------------------- backends


@runtime_checkable
class ApproverDirectory(Protocol):
    """Resolves approver ids to approvers (e.g. backed by an identity provider)."""

    def get(self, approver_id: str) -> Approver | None: ...


class InMemoryApproverDirectory:
    def __init__(self, approvers: Iterable[Approver] = ()) -> None:
        self._approvers: dict[str, Approver] = {}
        for approver in approvers:
            self.add(approver)

    def add(self, approver: Approver) -> Approver:
        if not isinstance(approver, Approver):
            raise ValidationError("expected an Approver")
        self._approvers[approver.id] = approver
        return approver

    def remove(self, approver_id: str) -> None:
        self._approvers.pop(approver_id, None)

    def get(self, approver_id: str) -> Approver | None:
        return self._approvers.get(approver_id)


@runtime_checkable
class ApprovalStore(Protocol):
    """Persistence for approval requests.

    Implementations must return copies (mutating a returned request must not change stored state), keep
    listing order stable (creation order) and keep cursors valid while items change status. The service
    serializes writes per request within one process; multi-process backends should add optimistic
    concurrency.
    """

    async def get(self, request_id: str) -> ApprovalRequest | None: ...

    async def put(self, request: ApprovalRequest) -> None: ...

    async def find_by_idempotency_key(self, key: str) -> ApprovalRequest | None: ...

    async def list(
        self, status: ApprovalStatus | None = None, limit: int = DEFAULT_PAGE_SIZE, cursor: str | None = None
    ) -> Page[ApprovalRequest]: ...


class InMemoryApprovalStore:
    """Dict-backed store with an idempotency-key index and a per-status index of creation sequence numbers.

    Cursors are the sequence number of the last returned item, so listing one status costs O(log n + page) and
    pages stay consistent while requests move between statuses.
    """

    def __init__(self) -> None:
        self._items: dict[str, ApprovalRequest] = {}
        self._seq_of: dict[str, int] = {}
        self._id_at: dict[int, str] = {}
        self._status_of: dict[str, ApprovalStatus] = {}
        self._all: list[int] = []
        self._by_status: dict[ApprovalStatus, list[int]] = {status: [] for status in ApprovalStatus}
        self._by_key: dict[str, str] = {}
        self._next_seq = 0

    def __len__(self) -> int:
        return len(self._items)

    def count(self, status: ApprovalStatus | None = None) -> int:
        return len(self._all if status is None else self._by_status[status])

    async def get(self, request_id: str) -> ApprovalRequest | None:
        item = self._items.get(request_id)
        return copy.deepcopy(item) if item is not None else None

    async def put(self, request: ApprovalRequest) -> None:
        key = request.idempotency_key
        if key is not None and self._by_key.get(key, request.id) != request.id:
            raise ValidationError(f"idempotency key {key!r} already belongs to another request")
        seq = self._seq_of.get(request.id)
        if seq is None:
            seq = self._next_seq
            self._next_seq += 1
            self._seq_of[request.id] = seq
            self._id_at[seq] = request.id
            self._all.append(seq)
            bisect.insort(self._by_status[request.status], seq)
        else:
            previous = self._status_of[request.id]
            if previous is not request.status:
                index = self._by_status[previous]
                del index[bisect.bisect_left(index, seq)]
                bisect.insort(self._by_status[request.status], seq)
        self._status_of[request.id] = request.status
        if key is not None:
            self._by_key[key] = request.id
        self._items[request.id] = copy.deepcopy(request)

    async def find_by_idempotency_key(self, key: str) -> ApprovalRequest | None:
        request_id = self._by_key.get(key)
        return await self.get(request_id) if request_id is not None else None

    async def list(
        self, status: ApprovalStatus | None = None, limit: int = DEFAULT_PAGE_SIZE, cursor: str | None = None
    ) -> Page[ApprovalRequest]:
        _validate_limit(limit)
        seqs = self._all if status is None else self._by_status[ApprovalStatus(status)]
        start = 0
        if cursor is not None:
            try:
                after = int(cursor)
            except (TypeError, ValueError):
                raise ValidationError(f"invalid cursor {cursor!r}") from None
            start = bisect.bisect_right(seqs, after)
        window = seqs[start : start + limit]
        items = tuple(copy.deepcopy(self._items[self._id_at[seq]]) for seq in window)
        next_cursor = str(window[-1]) if window and start + limit < len(seqs) else None
        return Page(items, next_cursor)


# --------------------------------------------------------------------------------------------- service


class ApprovalService:
    """Creates approval requests and drives them to a terminal status."""

    def __init__(
        self,
        store: ApprovalStore | None = None,
        directory: ApproverDirectory | None = None,
        clock: Clock | None = None,
        event_sink: EventSink | None = None,
        *,
        default_policy: ApprovalPolicy | None = None,
        sweep_batch_size: int = 100,
    ) -> None:
        self._store: ApprovalStore = store if store is not None else InMemoryApprovalStore()
        self._directory: ApproverDirectory = (
            directory if directory is not None else InMemoryApproverDirectory()
        )
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._sink: EventSink = event_sink if event_sink is not None else null_sink
        self._default_policy = default_policy if default_policy is not None else ApprovalPolicy()
        self._sweep_batch_size = _validate_limit(sweep_batch_size)
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._waiters: dict[str, set[asyncio.Future[None]]] = {}

    @property
    def store(self) -> ApprovalStore:
        return self._store

    @property
    def directory(self) -> ApproverDirectory:
        return self._directory

    @property
    def clock(self) -> Clock:
        return self._clock

    # ------------------------------------------------------------------ public API

    async def request(
        self,
        action: Action,
        requester: str,
        *,
        reason: str = "",
        context: Mapping[str, Any] | None = None,
        policy: ApprovalPolicy | None = None,
        idempotency_key: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ApprovalRequest:
        """Create a pending request (or return the one already created with ``idempotency_key``)."""
        if not isinstance(action, Action):
            raise ValidationError("action must be a hitl.core.Action")
        _require_text(requester, "requester")
        if policy is not None and not isinstance(policy, ApprovalPolicy):
            raise ValidationError("policy must be an ApprovalPolicy")
        ctx = _copy_mapping(context, "context")
        meta = _copy_mapping(metadata, "metadata")
        if idempotency_key is None:
            return await self._create(action, requester, reason, ctx, policy, None, meta)
        _require_text(idempotency_key, "idempotency_key")
        async with self._lock(f"idempotency:{idempotency_key}"):
            existing = await self._store.find_by_idempotency_key(idempotency_key)
            if existing is not None:
                if existing.action.fingerprint() != action.fingerprint():
                    raise ValidationError(
                        f"idempotency key {idempotency_key!r} was already used for a different action"
                    )
                return existing
            return await self._create(action, requester, reason, ctx, policy, idempotency_key, meta)

    async def get(self, request_id: str) -> ApprovalRequest:
        """Return the stored request or raise :class:`NotFoundError`."""
        request = await self._store.get(request_id)
        if request is None:
            raise NotFoundError(f"approval request {request_id!r} not found")
        return request

    async def list_requests(
        self, status: ApprovalStatus | None = None, limit: int = DEFAULT_PAGE_SIZE, cursor: str | None = None
    ) -> Page[ApprovalRequest]:
        return await self._store.list(status=status, limit=_validate_limit(limit), cursor=cursor)

    async def decide(
        self,
        request_id: str,
        approver_id: str,
        verdict: Verdict | str,
        *,
        comment: str = "",
        modifications: Mapping[str, Any] | None = None,
    ) -> ApprovalRequest:
        """Record one approver's decision and resolve the request on veto or quorum."""
        verdict = _coerce_verdict(verdict)
        mods = _copy_mapping(modifications, "modifications")
        if not isinstance(comment, str):
            raise ValidationError("comment must be a string")
        async with self._lock(request_id):
            request = await self.get(request_id)
            now = self._clock.now()
            await self._ensure_pending(request, now)
            request.verify_integrity()
            self._authorize(request, approver_id)
            if request.decided_by(approver_id):
                raise StateError(f"approver {approver_id!r} already decided request {request_id}")
            if verdict is Verdict.MODIFY and not mods:
                raise ValidationError("a MODIFY verdict requires non-empty modifications")
            if verdict is not Verdict.MODIFY and mods:
                raise ValidationError(f"a {verdict.name} verdict cannot carry modifications; use MODIFY")
            if verdict is Verdict.REJECT and request.policy.require_comment_on_reject and not comment.strip():
                raise ValidationError("this policy requires a comment when rejecting")

            request.decisions.append(Decision(approver_id, verdict, comment, mods, now))
            outcome: str | None = None
            if verdict is Verdict.REJECT:
                self._resolve(request, ApprovalStatus.REJECTED, now, approver_id, comment)
                outcome = "approval.rejected"
            elif request.approvals_count >= request.policy.required_approvals:
                self._resolve(request, ApprovalStatus.APPROVED, now, approver_id, comment)
                outcome = "approval.approved"
            await self._store.put(request)

            self._emit(
                "approval.decided",
                request,
                approver_id,
                approver=approver_id,
                verdict=verdict.value,
                comment=comment,
                modifications=copy.deepcopy(mods),
                approvals=request.approvals_count,
                required_approvals=request.policy.required_approvals,
            )
            if outcome == "approval.approved":
                self._emit(
                    outcome,
                    request,
                    approver_id,
                    approvals=request.approvals_count,
                    modifications=request.effective_modifications,
                )
            elif outcome == "approval.rejected":
                self._emit(outcome, request, approver_id, approver=approver_id, comment=comment)
            if outcome is not None:
                self._notify(request.id)
            return request

    async def cancel(self, request_id: str, actor: str, reason: str = "") -> ApprovalRequest:
        """Cancel a pending request; allowed for the requester and for currently eligible approvers."""
        _require_text(actor, "actor")
        async with self._lock(request_id):
            request = await self.get(request_id)
            now = self._clock.now()
            await self._ensure_pending(request, now)
            if actor != request.requester:
                approver = self._directory.get(actor)
                if approver is None or not approver.holds_any(request.eligible_roles):
                    raise AuthorizationError(f"{actor!r} may not cancel approval request {request_id}")
            self._resolve(request, ApprovalStatus.CANCELLED, now, actor, reason)
            await self._store.put(request)
            self._emit("approval.cancelled", request, actor, reason=reason)
            self._notify(request.id)
            return request

    async def expire_due(self, *, batch_size: int | None = None) -> list[ApprovalRequest]:
        """Escalate or expire every pending request past its deadline; returns the requests that changed."""
        batch = _validate_limit(batch_size) if batch_size is not None else self._sweep_batch_size
        now = self._clock.now()
        changed: list[ApprovalRequest] = []
        cursor: str | None = None
        while True:
            page = await self._store.list(status=ApprovalStatus.PENDING, limit=batch, cursor=cursor)
            for candidate in page.items:
                if not candidate.is_overdue(now):
                    continue
                async with self._lock(candidate.id):
                    request = await self._store.get(candidate.id)
                    if request is None or not request.is_overdue(now):
                        continue  # resolved or extended concurrently
                    await self._apply_timeout(request, now)
                    changed.append(request)
            if page.next_cursor is None:
                return changed
            cursor = page.next_cursor

    async def wait(self, request_id: str, timeout: float | None = None) -> ApprovalRequest:
        """Wait until the request is terminal; raises :class:`TimeoutError` when ``timeout`` elapses first."""
        if timeout is not None and (isinstance(timeout, bool) or timeout < 0):
            raise ValidationError("timeout must be a non-negative number or None")
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        waiters = self._waiters.setdefault(request_id, set())
        waiters.add(future)  # registered before reading, so a concurrent transition cannot be missed
        try:
            request = await self.get(request_id)
            if request.is_terminal:
                return request
            await asyncio.wait_for(future, timeout)
            return await self.get(request_id)
        finally:
            current = self._waiters.get(request_id)
            if current is not None:
                current.discard(future)
                if not current:
                    del self._waiters[request_id]

    async def pending_for(
        self, approver_id: str, limit: int = DEFAULT_PAGE_SIZE, cursor: str | None = None
    ) -> Page[ApprovalRequest]:
        """Pending, not overdue requests that ``approver_id`` may decide now."""
        _validate_limit(limit)
        approver = self._directory.get(approver_id)
        if approver is None:
            raise AuthorizationError(f"unknown approver {approver_id!r}")
        now = self._clock.now()
        items: list[ApprovalRequest] = []
        store_cursor = cursor
        while len(items) < limit:
            # Never fetch more than still needed, so the returned cursor cannot skip unseen matches.
            page = await self._store.list(
                status=ApprovalStatus.PENDING, limit=limit - len(items), cursor=store_cursor
            )
            items.extend(r for r in page.items if not r.is_overdue(now) and self._may_decide(r, approver))
            store_cursor = page.next_cursor
            if store_cursor is None:
                break
        return Page(tuple(items), store_cursor)

    # ------------------------------------------------------------------ internals

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def _create(
        self,
        action: Action,
        requester: str,
        reason: str,
        context: dict[str, Any],
        policy: ApprovalPolicy | None,
        idempotency_key: str | None,
        metadata: dict[str, Any],
    ) -> ApprovalRequest:
        policy = policy if policy is not None else self._default_policy
        now = self._clock.now()
        request = ApprovalRequest(
            id=new_id("apr"),
            action=action,
            requester=requester,
            policy=policy,
            reason=str(reason or ""),
            context=context,
            created_at=now,
            expires_at=now + policy.timeout_seconds if policy.timeout_seconds is not None else None,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )
        request.context_fingerprint = request.compute_fingerprint()
        await self._store.put(request)
        self._emit(
            "approval.requested",
            request,
            requester,
            requester=requester,
            reason=request.reason,
            risk=action.risk.name,
            required_approvals=policy.required_approvals,
            required_roles=sorted(policy.required_roles),
            expires_at=request.expires_at,
            context_fingerprint=request.context_fingerprint,
            idempotency_key=idempotency_key,
        )
        return request

    async def _ensure_pending(self, request: ApprovalRequest, now: float) -> None:
        """Apply an overdue timeout first, then require the request to still be pending."""
        if request.is_overdue(now):
            await self._apply_timeout(request, now)
        if request.status is ApprovalStatus.EXPIRED:
            raise ExpiredError(f"approval request {request.id} has expired")
        if request.status is not ApprovalStatus.PENDING:
            raise StateError(f"approval request {request.id} is {request.status.value}")

    async def _apply_timeout(self, request: ApprovalRequest, now: float) -> None:
        policy = request.policy
        if policy.escalation_roles and request.escalation_level == 0 and policy.timeout_seconds is not None:
            request.escalation_level += 1
            request.expires_at = now + policy.timeout_seconds
            await self._store.put(request)
            self._emit(
                "approval.escalated",
                request,
                SYSTEM_ACTOR,
                escalation_level=request.escalation_level,
                roles=sorted(request.eligible_roles),
                expires_at=request.expires_at,
            )
            return
        self._resolve(request, ApprovalStatus.EXPIRED, now, SYSTEM_ACTOR, "timeout")
        await self._store.put(request)
        self._emit("approval.expired", request, SYSTEM_ACTOR, escalation_level=request.escalation_level)
        self._notify(request.id)

    def _authorize(self, request: ApprovalRequest, approver_id: str) -> Approver:
        approver = self._directory.get(approver_id)
        if approver is None:
            raise AuthorizationError(f"unknown approver {approver_id!r}")
        if not approver.holds_any(request.eligible_roles):
            raise AuthorizationError(
                f"approver {approver_id!r} lacks every eligible role {sorted(request.eligible_roles)}"
            )
        if approver_id == request.requester and not request.policy.allow_self_approval:
            raise AuthorizationError(f"approver {approver_id!r} may not decide their own request")
        return approver

    @staticmethod
    def _may_decide(request: ApprovalRequest, approver: Approver) -> bool:
        if not approver.holds_any(request.eligible_roles) or request.decided_by(approver.id):
            return False
        return approver.id != request.requester or request.policy.allow_self_approval

    @staticmethod
    def _resolve(
        request: ApprovalRequest, status: ApprovalStatus, now: float, actor: str, reason: str
    ) -> None:
        request.status = status
        request.resolved_at = now
        request.resolved_by = actor
        request.resolution_reason = reason

    def _emit(self, kind: str, request: ApprovalRequest, actor: str, **data: Any) -> None:
        payload = {"action": request.action.name, "status": request.status.value, **data}
        event = Event(
            kind=kind, subject_id=request.id, actor=actor, data=payload, timestamp=self._clock.now()
        )
        try:
            self._sink(event)
        except Exception:
            logger.exception("event sink failed for %s on %s", kind, request.id)

    def _notify(self, request_id: str) -> None:
        for future in self._waiters.get(request_id, ()):
            if not future.done():
                future.set_result(None)


__all__ = [
    "ApprovalPolicy",
    "ApprovalRequest",
    "ApprovalService",
    "ApprovalStatus",
    "ApprovalStore",
    "Approver",
    "ApproverDirectory",
    "Decision",
    "InMemoryApprovalStore",
    "InMemoryApproverDirectory",
    "Page",
    "Verdict",
    "compute_context_fingerprint",
]
