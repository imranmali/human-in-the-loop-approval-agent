"""Tests for hitl.approvals; one or more tests per scenario of the approval-workflow spec."""

from __future__ import annotations

import asyncio

import pytest

from hitl.approvals import (
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    ApprovalStore,
    Approver,
    ApproverDirectory,
    Decision,
    InMemoryApprovalStore,
    InMemoryApproverDirectory,
    Page,
    Verdict,
)
from hitl.core import (
    Action,
    AuthorizationError,
    Event,
    ExpiredError,
    IntegrityError,
    ManualClock,
    NotFoundError,
    RiskLevel,
    StateError,
    ValidationError,
)

ACTION = Action("payments.refund", params={"amount": 100, "currency": "EUR"}, risk=RiskLevel.HIGH)


class YieldingStore(InMemoryApprovalStore):
    """Yields to the event loop on every call so concurrent tasks interleave."""

    async def get(self, request_id):
        await asyncio.sleep(0)
        return await super().get(request_id)

    async def put(self, request):
        await asyncio.sleep(0)
        await super().put(request)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(100.0)


@pytest.fixture
def events() -> list[Event]:
    return []


@pytest.fixture
def directory() -> InMemoryApproverDirectory:
    return InMemoryApproverDirectory(
        [
            Approver("alice", frozenset({"finance"})),
            Approver("bob", frozenset({"finance"})),
            Approver("carol", frozenset({"finance", "ops"})),
            Approver("dave", frozenset({"engineering"})),
            Approver("erin", frozenset({"director"})),
            Approver("agent", frozenset({"finance"})),
        ]
    )


@pytest.fixture
def service(clock, events, directory) -> ApprovalService:
    return ApprovalService(directory=directory, clock=clock, event_sink=events.append)


FINANCE = ApprovalPolicy(required_roles=frozenset({"finance"}))


def kinds(events: list[Event]) -> list[str]:
    return [e.kind for e in events]


# ----------------------------------------------------------------- policy validation


def test_quorum_below_one_is_rejected():
    with pytest.raises(ValidationError):
        ApprovalPolicy(required_approvals=0)
    with pytest.raises(ValidationError):
        ApprovalPolicy(required_approvals=True)


def test_non_positive_timeout_is_rejected():
    with pytest.raises(ValidationError):
        ApprovalPolicy(timeout_seconds=0)
    with pytest.raises(ValidationError):
        ApprovalPolicy(timeout_seconds=-5)


def test_escalation_without_timeout_is_rejected():
    with pytest.raises(ValidationError):
        ApprovalPolicy(escalation_roles=frozenset({"director"}))


def test_policy_coerces_roles():
    policy = ApprovalPolicy(required_roles="finance", escalation_roles=["director"], timeout_seconds=5)
    assert policy.required_roles == frozenset({"finance"})
    assert policy.escalation_roles == frozenset({"director"})
    assert policy.roles_for_level(0) == {"finance"} and policy.roles_for_level(1) == {"director"}
    with pytest.raises(ValidationError):
        Approver("")


# ----------------------------------------------------------------- creating requests


async def test_request_is_pending_with_deadline(service, clock):
    request = await service.request(
        ACTION, "agent", reason="refund > limit", policy=ApprovalPolicy(timeout_seconds=60)
    )
    assert request.status is ApprovalStatus.PENDING
    assert request.id.startswith("apr_")
    assert request.created_at == 100.0 and request.expires_at == 160.0
    assert request.reason == "refund > limit" and request.requester == "agent"
    stored = await service.get(request.id)
    assert stored == request


async def test_request_without_timeout_never_expires(service, clock):
    request = await service.request(ACTION, "agent")
    assert request.expires_at is None
    clock.advance(10**9)
    assert await service.expire_due() == []
    assert (await service.get(request.id)).status is ApprovalStatus.PENDING


async def test_context_is_copied_at_creation(service):
    context = {"assessment": {"score": 0.4, "reasons": ["low confidence"]}}
    request = await service.request(ACTION, "agent", context=context)
    context["assessment"]["reasons"].append("tampered")
    stored = await service.get(request.id)
    assert stored.context == {"assessment": {"score": 0.4, "reasons": ["low confidence"]}}
    stored.verify_integrity()


async def test_invalid_request_input_is_rejected(service):
    with pytest.raises(ValidationError):
        await service.request(ACTION, "")
    with pytest.raises(ValidationError):
        await service.request({"name": "x"}, "agent")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        await service.request(ACTION, "agent", context=["not", "a", "mapping"])  # type: ignore[arg-type]


async def test_get_unknown_request_raises(service):
    with pytest.raises(NotFoundError):
        await service.get("apr_missing")


# ----------------------------------------------------------------- idempotency


async def test_same_key_returns_existing_request(service):
    first = await service.request(ACTION, "agent", idempotency_key="run-1:step-3")
    second = await service.request(ACTION, "agent", idempotency_key="run-1:step-3")
    assert first.id == second.id
    assert len(service.store) == 1


async def test_same_key_with_different_action_is_rejected(service):
    await service.request(ACTION, "agent", idempotency_key="k")
    other = Action("payments.refund", params={"amount": 999})
    with pytest.raises(ValidationError):
        await service.request(other, "agent", idempotency_key="k")


async def test_concurrent_creation_with_same_key(clock, directory):
    service = ApprovalService(store=YieldingStore(), directory=directory, clock=clock)
    results = await asyncio.gather(*(service.request(ACTION, "agent", idempotency_key="k") for _ in range(5)))
    assert len({r.id for r in results}) == 1
    assert len(service.store) == 1


# ----------------------------------------------------------------- authorization


async def test_unknown_approver_is_refused(service):
    request = await service.request(ACTION, "agent")
    with pytest.raises(AuthorizationError):
        await service.decide(request.id, "mallory", Verdict.APPROVE)
    assert (await service.get(request.id)).decisions == []


async def test_approver_without_eligible_role_is_refused(service):
    request = await service.request(ACTION, "agent", policy=FINANCE)
    with pytest.raises(AuthorizationError):
        await service.decide(request.id, "dave", Verdict.APPROVE)


async def test_any_known_approver_when_no_roles_required(service):
    request = await service.request(ACTION, "agent")
    result = await service.decide(request.id, "dave", Verdict.APPROVE)
    assert result.status is ApprovalStatus.APPROVED


async def test_self_approval_is_refused_by_default(service):
    request = await service.request(ACTION, "agent", policy=FINANCE)
    with pytest.raises(AuthorizationError):
        await service.decide(request.id, "agent", Verdict.APPROVE)


async def test_self_approval_allowed_by_policy(service):
    policy = ApprovalPolicy(required_roles=frozenset({"finance"}), allow_self_approval=True)
    request = await service.request(ACTION, "agent", policy=policy)
    result = await service.decide(request.id, "agent", Verdict.APPROVE)
    assert result.status is ApprovalStatus.APPROVED
    assert [d.approver for d in result.decisions] == ["agent"]


# ----------------------------------------------------------------- recording decisions


async def test_decision_is_recorded(service, clock):
    request = await service.request(ACTION, "agent", policy=ApprovalPolicy(required_approvals=2))
    clock.advance(5)
    result = await service.decide(request.id, "alice", "approve", comment="looks fine")
    assert result.decisions == [Decision("alice", Verdict.APPROVE, "looks fine", {}, 105.0)]
    assert (await service.get(request.id)).decisions == result.decisions


async def test_same_approver_cannot_decide_twice(service):
    request = await service.request(ACTION, "agent", policy=ApprovalPolicy(required_approvals=2))
    await service.decide(request.id, "alice", Verdict.APPROVE)
    with pytest.raises(StateError):
        await service.decide(request.id, "alice", Verdict.APPROVE)


@pytest.mark.parametrize("terminal", ["approve", "reject", "cancel"])
async def test_decision_on_resolved_request_is_refused(service, terminal):
    request = await service.request(ACTION, "agent")
    if terminal == "cancel":
        await service.cancel(request.id, "agent")
    else:
        await service.decide(request.id, "alice", terminal, comment="no")
    with pytest.raises(StateError):
        await service.decide(request.id, "bob", Verdict.APPROVE)


async def test_decision_on_unknown_request(service):
    with pytest.raises(NotFoundError):
        await service.decide("apr_missing", "alice", Verdict.APPROVE)


# ----------------------------------------------------------------- decision input validation


async def test_modify_without_modifications_is_rejected(service):
    request = await service.request(ACTION, "agent")
    with pytest.raises(ValidationError):
        await service.decide(request.id, "alice", Verdict.MODIFY)
    with pytest.raises(ValidationError):
        await service.decide(request.id, "alice", Verdict.MODIFY, modifications={})


async def test_approve_with_modifications_is_rejected(service):
    request = await service.request(ACTION, "agent")
    with pytest.raises(ValidationError):
        await service.decide(request.id, "alice", Verdict.APPROVE, modifications={"amount": 1})


async def test_reject_without_comment_is_rejected(service):
    request = await service.request(ACTION, "agent")
    with pytest.raises(ValidationError):
        await service.decide(request.id, "alice", Verdict.REJECT, comment="   ")
    assert (await service.get(request.id)).status is ApprovalStatus.PENDING


async def test_reject_without_comment_allowed_by_policy(service):
    request = await service.request(ACTION, "agent", policy=ApprovalPolicy(require_comment_on_reject=False))
    result = await service.decide(request.id, "alice", Verdict.REJECT)
    assert result.status is ApprovalStatus.REJECTED


async def test_unknown_verdict(service):
    request = await service.request(ACTION, "agent")
    with pytest.raises(ValidationError):
        await service.decide(request.id, "alice", "maybe")


# ----------------------------------------------------------------- veto and quorum


async def test_rejection_after_partial_approval(service, clock):
    request = await service.request(ACTION, "agent", policy=ApprovalPolicy(required_approvals=2))
    await service.decide(request.id, "alice", Verdict.APPROVE)
    clock.advance(3)
    result = await service.decide(request.id, "bob", Verdict.REJECT, comment="amount too high")
    assert result.status is ApprovalStatus.REJECTED
    assert result.resolved_at == 103.0 and result.resolved_by == "bob"
    assert result.resolution_reason == "amount too high"


async def test_request_stays_pending_below_quorum(service):
    request = await service.request(ACTION, "agent", policy=ApprovalPolicy(required_approvals=2))
    result = await service.decide(request.id, "alice", Verdict.APPROVE)
    assert result.status is ApprovalStatus.PENDING
    assert result.approvals_count == 1 and result.resolved_at is None


async def test_request_approved_at_quorum(service, clock):
    request = await service.request(ACTION, "agent", policy=ApprovalPolicy(required_approvals=2))
    await service.decide(request.id, "alice", Verdict.APPROVE)
    clock.advance(1)
    result = await service.decide(request.id, "bob", Verdict.APPROVE)
    assert result.status is ApprovalStatus.APPROVED
    assert result.approvals_count == 2 and result.resolved_at == 101.0


async def test_merged_modifications(service):
    request = await service.request(ACTION, "agent", policy=ApprovalPolicy(required_approvals=2))
    await service.decide(request.id, "alice", Verdict.MODIFY, modifications={"amount": 50})
    result = await service.decide(
        request.id, "bob", Verdict.MODIFY, modifications={"amount": 40, "note": "x"}
    )
    assert result.status is ApprovalStatus.APPROVED
    assert result.effective_modifications == {"amount": 40, "note": "x"}
    effective = result.effective_action()
    assert effective.params == {"amount": 40, "currency": "EUR", "note": "x"}
    assert effective.name == ACTION.name and effective.risk is ACTION.risk
    assert result.action.params == {"amount": 100, "currency": "EUR"}


# ----------------------------------------------------------------- expiry and escalation


async def test_late_decision_expires_the_request(service, clock, events):
    request = await service.request(ACTION, "agent", policy=ApprovalPolicy(timeout_seconds=30))
    clock.advance(30)
    with pytest.raises(ExpiredError):
        await service.decide(request.id, "alice", Verdict.APPROVE)
    stored = await service.get(request.id)
    assert stored.status is ApprovalStatus.EXPIRED and stored.resolved_at == 130.0
    assert stored.decisions == []
    assert kinds(events)[-1] == "approval.expired"
    with pytest.raises(ExpiredError):
        await service.decide(request.id, "alice", Verdict.APPROVE)


ESCALATING = ApprovalPolicy(
    required_roles=frozenset({"finance"}), timeout_seconds=60, escalation_roles=frozenset({"director"})
)


async def test_first_timeout_escalates(service, clock):
    request = await service.request(ACTION, "agent", policy=ESCALATING)
    clock.advance(75)
    changed = await service.expire_due()
    assert [r.id for r in changed] == [request.id]
    stored = await service.get(request.id)
    assert stored.status is ApprovalStatus.PENDING
    assert stored.escalation_level == 1
    assert stored.expires_at == 175.0 + 60
    assert stored.eligible_roles == {"director"}


async def test_only_escalation_roles_may_decide_after_escalation(service, clock):
    request = await service.request(ACTION, "agent", policy=ESCALATING)
    clock.advance(60)
    await service.expire_due()
    with pytest.raises(AuthorizationError):
        await service.decide(request.id, "alice", Verdict.APPROVE)
    result = await service.decide(request.id, "erin", Verdict.APPROVE)
    assert result.status is ApprovalStatus.APPROVED


async def test_second_timeout_expires(service, clock, events):
    request = await service.request(ACTION, "agent", policy=ESCALATING)
    clock.advance(60)
    await service.expire_due()
    clock.advance(59)
    assert await service.expire_due() == []
    clock.advance(1)
    changed = await service.expire_due()
    assert [r.status for r in changed] == [ApprovalStatus.EXPIRED]
    assert (await service.get(request.id)).escalation_level == 1


async def test_late_decision_triggers_escalation(service, clock, events):
    request = await service.request(ACTION, "agent", policy=ESCALATING)
    clock.advance(90)
    result = await service.decide(request.id, "erin", Verdict.APPROVE)
    assert result.escalation_level == 1
    assert result.status is ApprovalStatus.APPROVED
    assert kinds(events) == [
        "approval.requested",
        "approval.escalated",
        "approval.decided",
        "approval.approved",
    ]


async def test_decisions_before_escalation_keep_counting(service, clock):
    policy = ApprovalPolicy(
        required_approvals=2,
        required_roles=frozenset({"finance"}),
        timeout_seconds=10,
        escalation_roles=frozenset({"director"}),
    )
    request = await service.request(ACTION, "agent", policy=policy)
    await service.decide(request.id, "alice", Verdict.APPROVE)
    clock.advance(10)
    result = await service.decide(request.id, "erin", Verdict.APPROVE)
    assert result.status is ApprovalStatus.APPROVED and result.approvals_count == 2


# ----------------------------------------------------------------- expiry sweep


async def test_sweep_handles_only_due_requests(service, clock):
    due = await service.request(ACTION, "agent", policy=ApprovalPolicy(timeout_seconds=10))
    later = await service.request(ACTION, "agent", policy=ApprovalPolicy(timeout_seconds=100))
    never = await service.request(ACTION, "agent")
    clock.advance(10)
    changed = await service.expire_due()
    assert [r.id for r in changed] == [due.id]
    assert changed[0].status is ApprovalStatus.EXPIRED
    assert (await service.get(later.id)).status is ApprovalStatus.PENDING
    assert (await service.get(never.id)).status is ApprovalStatus.PENDING


async def test_sweep_spans_several_batches(clock, directory):
    service = ApprovalService(directory=directory, clock=clock, sweep_batch_size=3)
    policy = ApprovalPolicy(timeout_seconds=5)
    created = [await service.request(ACTION, "agent", policy=policy) for _ in range(10)]
    keep = await service.request(ACTION, "agent")
    clock.advance(5)
    changed = await service.expire_due()
    assert sorted(r.id for r in changed) == sorted(r.id for r in created)
    store = service.store
    assert isinstance(store, InMemoryApprovalStore)
    assert store.count(ApprovalStatus.EXPIRED) == 10
    assert store.count(ApprovalStatus.PENDING) == 1
    assert (await service.get(keep.id)).status is ApprovalStatus.PENDING
    assert len(await service.expire_due(batch_size=1)) == 0


# ----------------------------------------------------------------- cancellation


async def test_requester_cancels(service, clock, events):
    request = await service.request(ACTION, "agent")
    clock.advance(2)
    result = await service.cancel(request.id, "agent", reason="no longer needed")
    assert result.status is ApprovalStatus.CANCELLED
    assert result.resolved_by == "agent" and result.resolution_reason == "no longer needed"
    assert result.resolved_at == 102.0
    assert events[-1].kind == "approval.cancelled" and events[-1].actor == "agent"
    assert events[-1].data["reason"] == "no longer needed"


async def test_eligible_approver_may_cancel(service):
    request = await service.request(ACTION, "agent", policy=FINANCE)
    result = await service.cancel(request.id, "alice")
    assert result.status is ApprovalStatus.CANCELLED


async def test_unrelated_actor_cannot_cancel(service):
    request = await service.request(ACTION, "agent", policy=FINANCE)
    with pytest.raises(AuthorizationError):
        await service.cancel(request.id, "dave")
    with pytest.raises(AuthorizationError):
        await service.cancel(request.id, "mallory")


async def test_resolved_request_cannot_be_cancelled(service):
    cancelled = await service.request(ACTION, "agent")
    await service.cancel(cancelled.id, "agent")
    with pytest.raises(StateError):
        await service.cancel(cancelled.id, "agent")
    approved = await service.request(ACTION, "agent")
    await service.decide(approved.id, "alice", Verdict.APPROVE)
    with pytest.raises(StateError):
        await service.cancel(approved.id, "agent")


async def test_cancel_overdue_request_raises_expired(service, clock):
    request = await service.request(ACTION, "agent", policy=ApprovalPolicy(timeout_seconds=1))
    clock.advance(1)
    with pytest.raises(ExpiredError):
        await service.cancel(request.id, "agent")


# ----------------------------------------------------------------- waiting


async def test_wait_resolves_on_decision(service):
    request = await service.request(ACTION, "agent")
    waiter = asyncio.create_task(service.wait(request.id, timeout=1))
    await asyncio.sleep(0)
    await service.decide(request.id, "alice", Verdict.APPROVE)
    result = await waiter
    assert result.status is ApprovalStatus.APPROVED
    assert service._waiters == {}


async def test_wait_on_terminal_request(service):
    request = await service.request(ACTION, "agent")
    await service.decide(request.id, "alice", Verdict.REJECT, comment="no")
    result = await service.wait(request.id, timeout=0)
    assert result.status is ApprovalStatus.REJECTED


async def test_wait_times_out(service):
    request = await service.request(ACTION, "agent")
    with pytest.raises(asyncio.TimeoutError):
        await service.wait(request.id, timeout=0.01)
    assert service._waiters == {}


async def test_wait_resolves_on_cancellation_and_expiry(service, clock):
    cancelled = await service.request(ACTION, "agent")
    expiring = await service.request(ACTION, "agent", policy=ApprovalPolicy(timeout_seconds=5))
    waiters = [asyncio.create_task(service.wait(r.id, timeout=1)) for r in (cancelled, expiring, expiring)]
    await asyncio.sleep(0)
    await service.cancel(cancelled.id, "agent")
    clock.advance(5)
    await service.expire_due()
    results = await asyncio.gather(*waiters)
    assert [r.status for r in results] == [
        ApprovalStatus.CANCELLED,
        ApprovalStatus.EXPIRED,
        ApprovalStatus.EXPIRED,
    ]


async def test_wait_unknown_request(service):
    with pytest.raises(NotFoundError):
        await service.wait("apr_missing", timeout=0)
    assert service._waiters == {}


# ----------------------------------------------------------------- listing


async def test_paging_through_pending_requests(service):
    created = [await service.request(ACTION, "agent") for _ in range(5)]
    pages: list[Page] = []
    cursor = None
    while True:
        page = await service.list_requests(ApprovalStatus.PENDING, limit=2, cursor=cursor)
        pages.append(page)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert [len(p.items) for p in pages] == [2, 2, 1]
    assert [r.id for p in pages for r in p.items] == [r.id for r in created]


async def test_listing_filters_by_status(service):
    approved = await service.request(ACTION, "agent")
    await service.request(ACTION, "agent")
    await service.decide(approved.id, "alice", Verdict.APPROVE)
    page = await service.list_requests(ApprovalStatus.APPROVED)
    assert [r.id for r in page.items] == [approved.id]
    assert page.next_cursor is None
    everything = await service.list_requests()
    assert len(everything.items) == 2


async def test_cursor_is_stable_while_items_change_status(service):
    created = [await service.request(ACTION, "agent") for _ in range(4)]
    first = await service.list_requests(ApprovalStatus.PENDING, limit=2)
    await service.decide(created[0].id, "alice", Verdict.APPROVE)
    second = await service.list_requests(ApprovalStatus.PENDING, limit=2, cursor=first.next_cursor)
    assert [r.id for r in second.items] == [created[2].id, created[3].id]


async def test_pending_work_of_an_approver(service, clock):
    finance = await service.request(ACTION, "agent", policy=FINANCE)
    ops_only = await service.request(
        ACTION, "agent", policy=ApprovalPolicy(required_roles=frozenset({"ops"}))
    )
    own = await service.request(ACTION, "alice", policy=FINANCE)
    decided = await service.request(ACTION, "agent", policy=ApprovalPolicy(required_approvals=2))
    await service.decide(decided.id, "alice", Verdict.APPROVE)
    overdue = await service.request(ACTION, "agent", policy=ApprovalPolicy(timeout_seconds=1))
    anyone = await service.request(ACTION, "agent")
    clock.advance(1)

    page = await service.pending_for("alice")
    assert [r.id for r in page.items] == [finance.id, anyone.id]
    assert page.next_cursor is None
    excluded = {ops_only.id, own.id, decided.id, overdue.id}
    assert not excluded & {r.id for r in page.items}

    carol = await service.pending_for("carol")
    assert ops_only.id in {r.id for r in carol.items}
    with pytest.raises(AuthorizationError):
        await service.pending_for("mallory")


async def test_pending_work_paging_does_not_skip(service):
    policy_ops = ApprovalPolicy(required_roles=frozenset({"ops"}))
    expected = []
    for i in range(7):
        req = await service.request(ACTION, "agent", policy=FINANCE if i % 2 == 0 else policy_ops)
        if i % 2 == 0:
            expected.append(req.id)
    seen: list[str] = []
    cursor = None
    while True:
        page = await service.pending_for("alice", limit=2, cursor=cursor)
        assert len(page.items) <= 2
        seen.extend(r.id for r in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == expected


async def test_invalid_paging_input(service):
    with pytest.raises(ValidationError):
        await service.list_requests(limit=0)
    with pytest.raises(ValidationError):
        await service.list_requests(cursor="not-a-cursor")
    with pytest.raises(ValidationError):
        await service.pending_for("alice", limit=0)


# ----------------------------------------------------------------- concurrency


async def test_concurrent_approvals_beyond_quorum(clock, directory):
    service = ApprovalService(store=YieldingStore(), directory=directory, clock=clock)
    request = await service.request(ACTION, "agent", policy=ApprovalPolicy(required_approvals=2))
    results = await asyncio.gather(
        *(service.decide(request.id, who, Verdict.APPROVE) for who in ("alice", "bob", "carol")),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 1 and isinstance(errors[0], StateError)
    stored = await service.get(request.id)
    assert stored.status is ApprovalStatus.APPROVED
    assert len(stored.decisions) == 2


async def test_concurrent_duplicate_decision(clock, directory):
    service = ApprovalService(store=YieldingStore(), directory=directory, clock=clock)
    request = await service.request(ACTION, "agent", policy=ApprovalPolicy(required_approvals=3))
    results = await asyncio.gather(
        service.decide(request.id, "alice", Verdict.APPROVE),
        service.decide(request.id, "alice", Verdict.APPROVE),
        return_exceptions=True,
    )
    assert sum(isinstance(r, StateError) for r in results) == 1
    assert len((await service.get(request.id)).decisions) == 1


# ----------------------------------------------------------------- integrity


async def test_tampered_context_is_detected(service):
    store = InMemoryApprovalStore()
    service = ApprovalService(store=store, directory=service.directory, clock=service.clock)
    request = await service.request(ACTION, "agent", context={"score": 0.2})
    tampered = await store.get(request.id)
    tampered.context["score"] = 0.99
    await store.put(tampered)
    with pytest.raises(IntegrityError):
        await service.decide(request.id, "alice", Verdict.APPROVE)
    assert (await service.get(request.id)).decisions == []


# ----------------------------------------------------------------- events


async def test_events_for_an_approval(service, clock, events):
    request = await service.request(ACTION, "agent", reason="why")
    clock.advance(7)
    await service.decide(request.id, "alice", Verdict.APPROVE, comment="ok")
    assert kinds(events) == ["approval.requested", "approval.decided", "approval.approved"]
    requested, decided, approved = events
    assert all(e.subject_id == request.id for e in events)
    assert requested.actor == "agent" and requested.timestamp == 100.0
    assert requested.data["action"] == "payments.refund" and requested.data["status"] == "pending"
    assert requested.data["risk"] == "HIGH"
    assert decided.actor == "alice" and decided.timestamp == 107.0
    assert decided.data["approver"] == "alice"
    assert decided.data["verdict"] == "approve" and decided.data["comment"] == "ok"
    assert decided.data["status"] == "approved"
    assert approved.data["status"] == "approved" and approved.data["action"] == "payments.refund"


async def test_events_for_rejection(service, events):
    request = await service.request(ACTION, "agent")
    await service.decide(request.id, "alice", Verdict.REJECT, comment="nope")
    assert kinds(events)[-1] == "approval.rejected"
    assert events[-1].data["comment"] == "nope" and events[-1].data["status"] == "rejected"


async def test_events_for_escalation_and_expiry(service, clock, events):
    await service.request(ACTION, "agent", policy=ESCALATING)
    clock.advance(60)
    await service.expire_due()
    clock.advance(60)
    await service.expire_due()
    escalated, expired = events[1:]
    assert (escalated.kind, expired.kind) == ("approval.escalated", "approval.expired")
    assert escalated.actor == expired.actor == "system"
    assert escalated.data["roles"] == ["director"] and escalated.data["escalation_level"] == 1
    assert escalated.data["status"] == "pending" and expired.data["status"] == "expired"
    assert expired.timestamp == 220.0


async def test_failing_sink_does_not_break_workflow(clock, directory):
    def broken(event: Event) -> None:
        raise RuntimeError("sink down")

    service = ApprovalService(directory=directory, clock=clock, event_sink=broken)
    request = await service.request(ACTION, "agent")
    result = await service.decide(request.id, "alice", Verdict.APPROVE)
    assert result.status is ApprovalStatus.APPROVED


# ----------------------------------------------------------------- serialization and protocols


async def test_round_trip(service):
    request = await service.request(
        ACTION,
        "agent",
        context={"assessment": {"score": 0.3}},
        policy=ApprovalPolicy(required_approvals=2, required_roles=frozenset({"finance"}), timeout_seconds=9),
        idempotency_key="k1",
        metadata={"run": "r1"},
    )
    result = await service.decide(request.id, "alice", Verdict.MODIFY, modifications={"amount": 10})
    data = result.to_dict()
    assert data["status"] == "pending" and data["approvals_count"] == 1
    assert data["effective_modifications"] == {"amount": 10}
    restored = ApprovalRequest.from_dict(data)
    assert restored == result
    restored.verify_integrity()


def test_in_memory_backends_satisfy_protocols():
    assert isinstance(InMemoryApprovalStore(), ApprovalStore)
    assert isinstance(InMemoryApproverDirectory(), ApproverDirectory)
    assert ApprovalStatus.PENDING.is_terminal is False
    assert all(s.is_terminal for s in ApprovalStatus if s is not ApprovalStatus.PENDING)


async def test_store_returns_copies():
    store = InMemoryApprovalStore()
    request = ApprovalRequest(id="apr_1", action=ACTION, requester="agent", policy=ApprovalPolicy())
    await store.put(request)
    request.context["x"] = 1
    fetched = await store.get("apr_1")
    assert fetched.context == {}
    fetched.decisions.append(Decision("alice", Verdict.APPROVE))
    assert (await store.get("apr_1")).decisions == []
    assert await store.get("apr_missing") is None
    assert await store.find_by_idempotency_key("nope") is None
