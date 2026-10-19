"""HITLAgent: the guarded execution loop composing detection, approval, checkpoints and audit.

assess -> (execute | pause -> request approval -> validate decision -> resume or abort)

All run state lives in the approval and checkpoint stores, so any worker sharing those stores
can resume a run another worker paused.
"""

from __future__ import annotations

import asyncio
import contextvars
import enum
import inspect
import logging
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .approvals import (
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    ApprovalStore,
    ApproverDirectory,
)
from .audit import AuditLog
from .checkpoint import CheckpointStatus, CheckpointStore, FieldSpec, InputSchema, PauseResumeManager
from .core import (
    Action,
    Clock,
    Event,
    EventBus,
    IntegrityError,
    NotFoundError,
    RiskLevel,
    StateError,
    SystemClock,
    ValidationError,
    new_id,
    validate_name,
)
from .uncertainty import Assessment, UncertaintyDetector, default_detector

logger = logging.getLogger("hitl.agent")

Handler = Callable[[Action, Mapping[str, Any]], Awaitable[Any] | Any]

_CURRENT_RUN: contextvars.ContextVar[str | None] = contextvars.ContextVar("hitl_run_id", default=None)


class RunStatus(enum.Enum):
    COMPLETED = "completed"
    PENDING_APPROVAL = "pending_approval"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    FAILED = "failed"


_TERMINAL_TO_RUN = {
    ApprovalStatus.REJECTED: RunStatus.REJECTED,
    ApprovalStatus.EXPIRED: RunStatus.EXPIRED,
    ApprovalStatus.CANCELLED: RunStatus.CANCELLED,
}


@dataclass(frozen=True)
class Outcome:
    """How a submitted run ended (or that it is waiting for a human)."""

    status: RunStatus
    run_id: str
    action: Action
    assessment: Assessment | None = None
    result: Any = None
    error: str | None = None
    request_id: str | None = None
    checkpoint_id: str | None = None
    modifications: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_pending(self) -> bool:
        return self.status is RunStatus.PENDING_APPROVAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "run_id": self.run_id,
            "action": self.action.to_dict(),
            "assessment": self.assessment.to_dict() if self.assessment else None,
            "result": self.result,
            "error": self.error,
            "request_id": self.request_id,
            "checkpoint_id": self.checkpoint_id,
            "modifications": dict(self.modifications),
        }


@dataclass(frozen=True)
class ActionRegistration:
    """A handler plus the review rules for one action name."""

    name: str
    handler: Handler
    modification_schema: InputSchema | None = None
    policy: ApprovalPolicy | None = None


def derive_modification_schema(params: Mapping[str, Any]) -> InputSchema:
    """Humans may change existing params (keeping their type) but never add new ones."""
    fields: dict[str, FieldSpec] = {}
    for key, value in params.items():
        if value is None or isinstance(value, bool):
            types: Any = object if value is None else bool
        elif isinstance(value, (int, float)):
            types = (int, float)
        elif isinstance(value, (list, tuple)):
            types = list
        else:
            types = type(value)
        fields[key] = FieldSpec(types, required=False)
    return InputSchema(fields, allow_extra=False)


def action_from_dict(data: Mapping[str, Any]) -> Action:
    return Action(
        name=data["name"],
        params=dict(data.get("params", {})),
        risk=RiskLevel[data.get("risk", "LOW")],
        confidence=float(data.get("confidence", 1.0)),
        metadata=dict(data.get("metadata", {})),
    )


class _RunCorrelator:
    """EventSink wrapper that stamps ``run_id`` onto events so the audit trail groups by run.

    The run id comes from the event itself, the run currently executing in this task, or a bounded
    map of approval-request / checkpoint ids registered by the agent (for decisions made elsewhere).
    """

    def __init__(self, downstream: Callable[[Event], None], max_tracked: int = 100_000) -> None:
        self._downstream = downstream
        self._subjects: OrderedDict[str, str] = OrderedDict()
        self._max_tracked = max_tracked

    def track(self, subject_id: str, run_id: str) -> None:
        self._subjects[subject_id] = run_id
        self._subjects.move_to_end(subject_id)
        while len(self._subjects) > self._max_tracked:
            self._subjects.popitem(last=False)

    def __call__(self, event: Event) -> None:
        if "run_id" not in event.data:
            run_id = _CURRENT_RUN.get() or self._subjects.get(event.subject_id)
            if run_id is not None:
                event = replace(event, data={**event.data, "run_id": run_id})
        self._downstream(event)


class HITLAgent:
    """Executes actions, pausing for human approval whenever the detector is uncertain."""

    def __init__(
        self,
        *,
        detector: UncertaintyDetector | None = None,
        audit: AuditLog | None = None,
        approval_store: ApprovalStore | None = None,
        directory: ApproverDirectory | None = None,
        checkpoint_store: CheckpointStore | None = None,
        default_policy: ApprovalPolicy | None = None,
        checkpoint_ttl: float | None = None,
        clock: Clock | None = None,
        bus: EventBus | None = None,
        requester: str = "agent",
    ) -> None:
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.bus = bus if bus is not None else EventBus()
        self.audit = audit if audit is not None else AuditLog(clock=self.clock)
        self.bus.subscribe(self.audit)
        self._sink = _RunCorrelator(self.bus.emit)
        self.detector = detector if detector is not None else default_detector(clock=self.clock)
        self.detector.event_sink = self._sink
        self.approvals = ApprovalService(
            approval_store, directory, self.clock, self._sink, default_policy=default_policy
        )
        self.checkpoints = PauseResumeManager(
            checkpoint_store, self.clock, self._sink, default_ttl=checkpoint_ttl
        )
        self.requester = requester
        self._handlers: dict[str, ActionRegistration] = {}
        self._submit_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    # ------------------------------------------------------------------ registration

    def register(
        self,
        name: str,
        handler: Handler,
        *,
        modification_schema: InputSchema | None = None,
        policy: ApprovalPolicy | None = None,
    ) -> ActionRegistration:
        validate_name(name, "action name")
        if not callable(handler):
            raise ValidationError(f"handler for {name!r} must be callable")
        registration = ActionRegistration(name, handler, modification_schema, policy)
        self._handlers[name] = registration
        return registration

    def action(self, name: str, **kwargs: Any) -> Callable[[Handler], Handler]:
        """Decorator form of :meth:`register`."""

        def decorate(handler: Handler) -> Handler:
            self.register(name, handler, **kwargs)
            return handler

        return decorate

    def _registration(self, name: str) -> ActionRegistration:
        try:
            return self._handlers[name]
        except KeyError:
            raise NotFoundError(f"no handler registered for action {name!r}") from None

    # ------------------------------------------------------------------ execution loop

    async def submit(
        self,
        action: Action,
        *,
        run_id: str | None = None,
        context: Mapping[str, Any] | None = None,
        requester: str | None = None,
    ) -> Outcome:
        """Assess ``action``; execute it now, or pause and request human approval."""
        if not isinstance(action, Action):
            raise ValidationError("action must be a hitl.core.Action")
        registration = self._registration(action.name)
        run_id = run_id or new_id("run")
        ctx = dict(context or {})
        requester = requester or self.requester
        token = _CURRENT_RUN.set(run_id)
        try:
            self._emit("agent.submitted", run_id, requester, action=action.name, requester=requester)
            assessment = self.detector.assess(action, ctx)
            if not assessment.requires_review:
                return await self._execute(run_id, action, ctx, assessment)
            return await self._pause(run_id, action, ctx, assessment, registration, requester)
        finally:
            _CURRENT_RUN.reset(token)

    async def _pause(
        self,
        run_id: str,
        action: Action,
        ctx: dict[str, Any],
        assessment: Assessment,
        registration: ActionRegistration,
        requester: str,
    ) -> Outcome:
        key = f"{run_id}:{action.fingerprint()}"
        lock = self._submit_locks.get(key)
        if lock is None:
            lock = self._submit_locks[key] = asyncio.Lock()
        async with lock:
            existing = await self.approvals.store.find_by_idempotency_key(key)
            if existing is not None:
                return self._pending(run_id, action, assessment, existing)
            schema = registration.modification_schema or derive_modification_schema(action.params)
            checkpoint = await self.checkpoints.pause(
                run_id,
                action.name,
                {
                    "action": action.to_dict(),
                    "context": ctx,
                    "assessment": assessment.to_dict(),
                    "requester": requester,
                },
                input_schema=schema,
                metadata={"action_fingerprint": action.fingerprint()},
            )
            self._sink.track(checkpoint.id, run_id)
            request = await self.approvals.request(
                action,
                requester,
                reason="; ".join(assessment.reasons),
                context={
                    "run_id": run_id,
                    "checkpoint_id": checkpoint.id,
                    "assessment": assessment.to_dict(),
                    "modification_schema": schema.to_dict(),
                },
                policy=registration.policy,
                idempotency_key=key,
                metadata={"run_id": run_id, "checkpoint_id": checkpoint.id},
            )
            self._sink.track(request.id, run_id)
            self._emit(
                "agent.paused",
                run_id,
                requester,
                action=action.name,
                request_id=request.id,
                checkpoint_id=checkpoint.id,
                score=assessment.score,
                reasons=list(assessment.reasons),
            )
            return self._pending(run_id, action, assessment, request)

    @staticmethod
    def _pending(run_id: str, action: Action, assessment: Assessment, request: ApprovalRequest) -> Outcome:
        return Outcome(
            RunStatus.PENDING_APPROVAL,
            run_id,
            action,
            assessment,
            request_id=request.id,
            checkpoint_id=request.metadata.get("checkpoint_id"),
        )

    async def resume(self, request_id: str, *, actor: str = "system") -> Outcome:
        """Continue a paused run once its approval request reached a terminal status."""
        request = await self.approvals.get(request_id)
        run_id = request.metadata.get("run_id")
        checkpoint_id = request.metadata.get("checkpoint_id")
        if not run_id or not checkpoint_id:
            raise StateError(f"approval request {request_id!r} was not created by HITLAgent")
        token = _CURRENT_RUN.set(run_id)
        try:
            if not request.is_terminal:
                raise StateError(f"approval request {request_id!r} is still {request.status.value}")
            checkpoint = await self.checkpoints.get(checkpoint_id)
            if checkpoint.status is not CheckpointStatus.ACTIVE:
                raise StateError(f"run {run_id!r} was already resumed or closed ({checkpoint.status.value})")
            stored_action = action_from_dict(checkpoint.state["action"])
            if stored_action.fingerprint() != request.action.fingerprint():
                self._emit("agent.integrity_failed", run_id, actor, request_id=request_id)
                raise IntegrityError(f"checkpointed action for run {run_id!r} does not match the approval")
            assessment = _assessment_from(checkpoint.state.get("assessment"))

            if request.status is not ApprovalStatus.APPROVED:
                status = _TERMINAL_TO_RUN[request.status]
                await self.checkpoints.discard(checkpoint_id, actor, reason=request.status.value)
                self._emit(f"agent.{status.value}", run_id, actor, action=stored_action.name)
                return Outcome(
                    status,
                    run_id,
                    stored_action,
                    assessment,
                    error=request.resolution_reason or None,
                    request_id=request_id,
                    checkpoint_id=checkpoint_id,
                )

            resumed = await self.checkpoints.resume(
                checkpoint_id, request.effective_modifications, actor=actor
            )
            modifications = dict(resumed.human_input)
            action = replace(stored_action, params={**stored_action.params, **modifications})
            return await self._execute(
                run_id,
                action,
                dict(resumed.state.get("context", {})),
                assessment,
                request_id=request_id,
                checkpoint_id=checkpoint_id,
                modifications=modifications,
            )
        finally:
            _CURRENT_RUN.reset(token)

    async def run(
        self,
        action: Action,
        *,
        timeout: float | None = None,
        run_id: str | None = None,
        context: Mapping[str, Any] | None = None,
        requester: str | None = None,
    ) -> Outcome:
        """Submit, wait up to ``timeout`` for a human decision, then resume."""
        outcome = await self.submit(action, run_id=run_id, context=context, requester=requester)
        if not outcome.is_pending or outcome.request_id is None:
            return outcome
        try:
            await self.approvals.wait(outcome.request_id, timeout=timeout)
        except TimeoutError:
            return outcome
        return await self.resume(outcome.request_id)

    async def process_decided(self, *, batch_size: int = 100, actor: str = "system") -> list[Outcome]:
        """Expire overdue requests, then resume every decided run whose checkpoint is still active."""
        await self.approvals.expire_due()
        outcomes: list[Outcome] = []
        for status in (
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.CANCELLED,
        ):
            cursor: str | None = None
            while True:
                page = await self.approvals.list_requests(status=status, limit=batch_size, cursor=cursor)
                for request in page.items:
                    checkpoint_id = request.metadata.get("checkpoint_id")
                    if not checkpoint_id:
                        continue
                    try:
                        checkpoint = await self.checkpoints.get(checkpoint_id)
                        if checkpoint.status is not CheckpointStatus.ACTIVE:
                            continue
                        outcomes.append(await self.resume(request.id, actor=actor))
                    except (StateError, IntegrityError, ValidationError, NotFoundError) as exc:
                        logger.warning("could not resume approval %s: %s", request.id, exc)
                cursor = page.next_cursor
                if cursor is None:
                    break
        return outcomes

    async def _execute(
        self,
        run_id: str,
        action: Action,
        ctx: Mapping[str, Any],
        assessment: Assessment | None,
        *,
        request_id: str | None = None,
        checkpoint_id: str | None = None,
        modifications: Mapping[str, Any] | None = None,
    ) -> Outcome:
        registration = self._registration(action.name)
        mods = dict(modifications or {})
        common = {
            "request_id": request_id,
            "checkpoint_id": checkpoint_id,
            "modifications": mods,
        }
        try:
            result = registration.handler(action, ctx)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 - contained and reported as FAILED
            error = f"{type(exc).__name__}: {exc}"
            self._emit("agent.failed", run_id, "system", action=action.name, error=error, **common)
            return Outcome(RunStatus.FAILED, run_id, action, assessment, error=error, **common)
        self.detector.observe(action)
        self._emit(
            "agent.executed",
            run_id,
            "system",
            action=action.name,
            action_fingerprint=action.fingerprint(),
            reviewed=request_id is not None,
            **common,
        )
        return Outcome(RunStatus.COMPLETED, run_id, action, assessment, result=result, **common)

    def _emit(self, kind: str, run_id: str, actor: str, **data: Any) -> None:
        self._sink(
            Event(
                kind,
                subject_id=run_id,
                actor=actor,
                data={"run_id": run_id, **data},
                timestamp=self.clock.now(),
            )
        )

    # ------------------------------------------------------------------ queries

    def history(self, run_id: str) -> list[Any]:
        """Audit records for a run, in order."""
        return self.audit.query(correlation_id=run_id, limit=10_000)


def _assessment_from(data: Any) -> Assessment | None:
    """Assessments are checkpointed as dicts; rebuild a light-weight copy for the outcome."""
    if not isinstance(data, Mapping):
        return None
    try:
        return Assessment(
            id=data["id"],
            action_name=data["action_name"],
            action_fingerprint=data["action_fingerprint"],
            risk=RiskLevel[data["risk"]],
            score=data["score"],
            threshold=data["threshold"],
            requires_review=data["requires_review"],
            reasons=tuple(data.get("reasons", ())),
            signal_results=(),
            created_at=data["created_at"],
            aggregation=data.get("aggregation", "max"),
        )
    except (KeyError, TypeError, ValueError):
        return None


__all__ = [
    "ActionRegistration",
    "HITLAgent",
    "Outcome",
    "RunStatus",
    "action_from_dict",
    "derive_modification_schema",
]
