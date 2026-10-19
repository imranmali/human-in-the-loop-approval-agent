import asyncio
import json

import pytest

from hitl import (
    Action,
    ApprovalPolicy,
    ApprovalStatus,
    Approver,
    CheckpointStatus,
    ConfidenceSignal,
    FieldSpec,
    HITLAgent,
    InMemoryApprovalStore,
    InMemoryApproverDirectory,
    InMemoryCheckpointStore,
    InputSchema,
    IntegrityError,
    ManualClock,
    NotFoundError,
    RiskLevel,
    RiskSignal,
    RunStatus,
    StateError,
    UncertaintyDetector,
    ValidationError,
    default_detector,
    derive_modification_schema,
)


class Recorder:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def __call__(self, action, context):
        self.calls.append((action, dict(context)))
        if self.fail:
            raise RuntimeError("downstream unavailable")
        return {"done": action.name, "params": dict(action.params)}


def make_agent(**kw):
    clock = kw.pop("clock", ManualClock(1_000))
    directory = kw.pop(
        "directory",
        InMemoryApproverDirectory([Approver("alice", {"ops"}), Approver("bob", {"ops"})]),
    )
    detector = kw.pop(
        "detector",
        UncertaintyDetector([ConfidenceSignal(floor=0.6), RiskSignal(RiskLevel.HIGH)], threshold=0.9),
    )
    agent = HITLAgent(clock=clock, directory=directory, detector=detector, **kw)
    handler = Recorder()
    agent.register("payments.refund", handler)
    return agent, handler, clock


SAFE = Action("payments.refund", {"amount": 10, "currency": "EUR"}, RiskLevel.LOW, 0.95)
RISKY = Action("payments.refund", {"amount": 5000, "currency": "EUR"}, RiskLevel.HIGH, 0.95)


# --------------------------------------------------------------------------- guarded execution


async def test_confident_low_risk_action_runs_immediately():
    agent, handler, _ = make_agent()
    outcome = await agent.submit(SAFE, context={"ticket": "T-1"})
    assert outcome.status is RunStatus.COMPLETED
    assert outcome.result == {"done": "payments.refund", "params": {"amount": 10, "currency": "EUR"}}
    assert handler.calls == [(SAFE, {"ticket": "T-1"})]
    assert outcome.request_id is None and outcome.assessment.requires_review is False


async def test_uncertain_action_pauses_without_executing():
    agent, handler, _ = make_agent()
    outcome = await agent.submit(RISKY)
    assert outcome.status is RunStatus.PENDING_APPROVAL and outcome.is_pending
    assert handler.calls == []
    request = await agent.approvals.get(outcome.request_id)
    checkpoint = await agent.checkpoints.get(outcome.checkpoint_id)
    assert request.status is ApprovalStatus.PENDING
    assert request.metadata == {"run_id": outcome.run_id, "checkpoint_id": checkpoint.id}
    assert checkpoint.status is CheckpointStatus.ACTIVE and checkpoint.run_id == outcome.run_id
    assert "risk" in request.reason.lower()


async def test_low_confidence_also_pauses():
    agent, handler, _ = make_agent()
    outcome = await agent.submit(Action("payments.refund", {"amount": 1}, RiskLevel.LOW, 0.2))
    assert outcome.is_pending and handler.calls == []


async def test_unregistered_action_raises_before_assessment():
    agent, _, _ = make_agent()
    with pytest.raises(NotFoundError):
        await agent.submit(Action("unknown.action"))
    assert len(agent.audit) == 0


async def test_submit_rejects_non_action():
    agent, _, _ = make_agent()
    with pytest.raises(ValidationError):
        await agent.submit({"name": "payments.refund"})


# --------------------------------------------------------------------------- resume


async def test_approved_run_executes_original_action():
    agent, handler, _ = make_agent()
    paused = await agent.submit(RISKY, context={"ticket": "T-9"})
    await agent.approvals.decide(paused.request_id, "alice", "approve")
    outcome = await agent.resume(paused.request_id, actor="alice")
    assert outcome.status is RunStatus.COMPLETED
    assert handler.calls == [(RISKY, {"ticket": "T-9"})]
    assert outcome.run_id == paused.run_id and outcome.modifications == {}
    assert (await agent.checkpoints.get(paused.checkpoint_id)).status is CheckpointStatus.RESUMED


async def test_modifications_are_validated_and_applied():
    agent, handler, _ = make_agent()
    paused = await agent.submit(RISKY)
    await agent.approvals.decide(paused.request_id, "alice", "modify", modifications={"amount": 250})
    outcome = await agent.resume(paused.request_id)
    assert outcome.status is RunStatus.COMPLETED
    assert outcome.modifications == {"amount": 250}
    assert outcome.action.params == {"amount": 250, "currency": "EUR"}
    assert handler.calls[0][0].params["amount"] == 250


@pytest.mark.parametrize("bad", [{"injected": "rm -rf"}, {"amount": "a lot"}, {"currency": 3}])
async def test_invalid_modifications_never_execute_and_stay_resumable(bad):
    agent, handler, _ = make_agent()
    paused = await agent.submit(RISKY)
    await agent.approvals.decide(paused.request_id, "alice", "modify", modifications=bad)
    with pytest.raises(ValidationError):
        await agent.resume(paused.request_id)
    assert handler.calls == []
    assert (await agent.checkpoints.get(paused.checkpoint_id)).status is CheckpointStatus.ACTIVE
    kinds = [r.kind for r in agent.history(paused.run_id)]
    assert "checkpoint.resume_rejected" in kinds


async def test_explicit_modification_schema_is_enforced():
    agent, handler, _ = make_agent()
    schema = InputSchema({"amount": FieldSpec(int, required=False, min=1, max=1000)})
    agent.register("payments.refund", handler, modification_schema=schema)
    paused = await agent.submit(RISKY)
    await agent.approvals.decide(paused.request_id, "alice", "modify", modifications={"amount": 5001})
    with pytest.raises(ValidationError):
        await agent.resume(paused.request_id)
    request = await agent.approvals.get(paused.request_id)
    assert request.context["modification_schema"]["fields"]["amount"]["max"] == 1000


async def test_rejected_run_never_executes():
    agent, handler, _ = make_agent()
    paused = await agent.submit(RISKY)
    await agent.approvals.decide(paused.request_id, "alice", "reject", comment="customer already refunded")
    outcome = await agent.resume(paused.request_id)
    assert outcome.status is RunStatus.REJECTED and handler.calls == []
    assert outcome.error == "customer already refunded"
    assert (await agent.checkpoints.get(paused.checkpoint_id)).status is CheckpointStatus.DISCARDED


async def test_expired_run_never_executes():
    agent, handler, clock = make_agent(default_policy=ApprovalPolicy(timeout_seconds=60))
    paused = await agent.submit(RISKY)
    clock.advance(61)
    outcomes = await agent.process_decided()
    assert [(o.run_id, o.status) for o in outcomes] == [(paused.run_id, RunStatus.EXPIRED)]
    assert handler.calls == []


async def test_cancelled_run_never_executes():
    agent, handler, _ = make_agent()
    paused = await agent.submit(RISKY)
    await agent.approvals.cancel(paused.request_id, "agent", reason="superseded")
    outcome = await agent.resume(paused.request_id)
    assert outcome.status is RunStatus.CANCELLED and handler.calls == []


async def test_resume_while_pending_raises():
    agent, handler, _ = make_agent()
    paused = await agent.submit(RISKY)
    with pytest.raises(StateError):
        await agent.resume(paused.request_id)
    assert handler.calls == []


async def test_run_resumes_only_once():
    agent, handler, _ = make_agent()
    paused = await agent.submit(RISKY)
    await agent.approvals.decide(paused.request_id, "alice", "approve")
    await agent.resume(paused.request_id)
    with pytest.raises(StateError):
        await agent.resume(paused.request_id)
    assert len(handler.calls) == 1


async def test_concurrent_resumes_execute_once():
    agent, handler, _ = make_agent()
    paused = await agent.submit(RISKY)
    await agent.approvals.decide(paused.request_id, "alice", "approve")
    results = await asyncio.gather(
        *(agent.resume(paused.request_id) for _ in range(5)), return_exceptions=True
    )
    assert sum(1 for r in results if not isinstance(r, Exception)) == 1
    assert all(isinstance(r, StateError) for r in results if isinstance(r, Exception))
    assert len(handler.calls) == 1


async def test_resume_rejects_foreign_request():
    agent, _, _ = make_agent()
    request = await agent.approvals.request(RISKY, "someone")
    await agent.approvals.decide(request.id, "alice", "approve")
    with pytest.raises(StateError):
        await agent.resume(request.id)


# --------------------------------------------------------------------------- binding


async def test_tampered_checkpoint_action_is_refused():
    store = InMemoryCheckpointStore()
    agent, handler, _ = make_agent(checkpoint_store=store)
    paused = await agent.submit(RISKY)
    await agent.approvals.decide(paused.request_id, "alice", "approve")
    store._items[paused.checkpoint_id].state["action"]["params"]["amount"] = 999_999
    with pytest.raises(IntegrityError):
        await agent.resume(paused.request_id)
    assert handler.calls == []
    assert "agent.integrity_failed" in [r.kind for r in agent.history(paused.run_id)]


# --------------------------------------------------------------------------- idempotency


async def test_duplicate_submit_returns_same_request():
    agent, _, _ = make_agent()
    first = await agent.submit(RISKY, run_id="run-1")
    second = await agent.submit(RISKY, run_id="run-1")
    assert first.request_id == second.request_id
    assert first.checkpoint_id == second.checkpoint_id


async def test_concurrent_duplicate_submits_create_one_request():
    agent, _, _ = make_agent()
    outcomes = await asyncio.gather(*(agent.submit(RISKY, run_id="run-1") for _ in range(5)))
    assert len({o.request_id for o in outcomes}) == 1
    assert len(await agent.checkpoints.active_for_run("run-1")) == 1


async def test_different_runs_get_separate_requests():
    agent, _, _ = make_agent()
    a = await agent.submit(RISKY)
    b = await agent.submit(RISKY)
    assert a.run_id != b.run_id and a.request_id != b.request_id


# --------------------------------------------------------------------------- blocking run


async def test_run_waits_for_decision():
    agent, handler, _ = make_agent()
    task = asyncio.create_task(agent.run(RISKY, timeout=5))
    for _ in range(50):
        page = await agent.approvals.list_requests(status=ApprovalStatus.PENDING)
        if page.items:
            break
        await asyncio.sleep(0)
    await agent.approvals.decide(page.items[0].id, "bob", "approve")
    outcome = await task
    assert outcome.status is RunStatus.COMPLETED and len(handler.calls) == 1


async def test_run_times_out_as_pending_then_resumes_later():
    agent, handler, _ = make_agent()
    outcome = await agent.run(RISKY, timeout=0.01)
    assert outcome.status is RunStatus.PENDING_APPROVAL and handler.calls == []
    await agent.approvals.decide(outcome.request_id, "alice", "approve")
    assert (await agent.resume(outcome.request_id)).status is RunStatus.COMPLETED


async def test_run_returns_immediately_when_no_review_needed():
    agent, handler, _ = make_agent()
    assert (await agent.run(SAFE, timeout=0.01)).status is RunStatus.COMPLETED


# --------------------------------------------------------------------------- sweep / scale-out


async def test_process_decided_resumes_only_decided_runs():
    agent, handler, _ = make_agent()
    runs = [await agent.submit(RISKY) for _ in range(3)]
    await agent.approvals.decide(runs[0].request_id, "alice", "approve")
    await agent.approvals.decide(runs[1].request_id, "alice", "approve")
    outcomes = await agent.process_decided(batch_size=1)
    assert sorted(o.run_id for o in outcomes) == sorted([runs[0].run_id, runs[1].run_id])
    assert all(o.status is RunStatus.COMPLETED for o in outcomes)
    assert await agent.process_decided() == []
    assert len(handler.calls) == 2


async def test_another_worker_sharing_stores_can_resume():
    clock = ManualClock(0)
    approval_store, checkpoint_store = InMemoryApprovalStore(), InMemoryCheckpointStore()
    shared = {"clock": clock, "approval_store": approval_store, "checkpoint_store": checkpoint_store}
    worker_a, handler_a, _ = make_agent(**shared)
    worker_b, handler_b, _ = make_agent(**shared)
    paused = await worker_a.submit(RISKY)
    await worker_b.approvals.decide(paused.request_id, "alice", "approve")
    outcomes = await worker_b.process_decided()
    assert [o.run_id for o in outcomes] == [paused.run_id]
    assert handler_a.calls == [] and len(handler_b.calls) == 1


# --------------------------------------------------------------------------- failures


async def test_handler_failure_is_contained():
    agent, _, _ = make_agent()
    agent.register("payments.refund", Recorder(fail=True))
    outcome = await agent.submit(SAFE)
    assert outcome.status is RunStatus.FAILED
    assert outcome.error == "RuntimeError: downstream unavailable"
    assert [r.kind for r in agent.history(outcome.run_id)][-1] == "agent.failed"


async def test_sync_handlers_and_decorator_registration():
    agent, _, _ = make_agent()

    @agent.action("notes.add")
    def add_note(action, context):
        return action.params["text"].upper()

    outcome = await agent.submit(Action("notes.add", {"text": "hi"}))
    assert outcome.result == "HI"


def test_register_validates_inputs():
    agent, _, _ = make_agent()
    with pytest.raises(ValidationError):
        agent.register("Bad Name", Recorder())
    with pytest.raises(ValidationError):
        agent.register("ok.name", "not callable")


# --------------------------------------------------------------------------- audit trail


async def test_full_history_for_a_run_is_audited_and_verifies():
    agent, _, _ = make_agent()
    paused = await agent.submit(RISKY)
    await agent.approvals.decide(paused.request_id, "alice", "approve")
    await agent.resume(paused.request_id, actor="alice")
    kinds = [r.kind for r in agent.history(paused.run_id)]
    expected = [
        "agent.submitted",
        "uncertainty.assessed",
        "uncertainty.review_required",
        "checkpoint.paused",
        "approval.requested",
        "agent.paused",
        "approval.decided",
        "approval.approved",
        "checkpoint.resumed",
        "agent.executed",
    ]
    assert kinds == expected
    assert agent.audit.verify().ok
    decided = [r for r in agent.history(paused.run_id) if r.kind == "approval.decided"][0]
    assert decided.actor == "alice"


async def test_audit_trail_detects_tampering():
    agent, _, _ = make_agent()
    await agent.submit(SAFE)
    backend = agent.audit.backend
    import dataclasses

    backend._records[1] = dataclasses.replace(backend._records[1], actor="mallory")
    report = agent.audit.verify()
    assert not report.ok and report.first_bad_seq == 1


# --------------------------------------------------------------------------- helpers


async def test_default_detector_learns_after_approved_execution():
    agent, handler, _ = make_agent(detector=default_detector())
    action = Action("payments.refund", {"amount": 10}, RiskLevel.LOW, 0.99)
    first = await agent.submit(action)
    assert first.is_pending  # never seen this shape before
    await agent.approvals.decide(first.request_id, "alice", "approve")
    await agent.resume(first.request_id)
    second = await agent.submit(Action("payments.refund", {"amount": 12}, RiskLevel.LOW, 0.99))
    assert second.status is RunStatus.COMPLETED


def test_derived_schema_allows_only_existing_keys_with_same_type():
    schema = derive_modification_schema({"amount": 10, "note": "x", "flag": True, "tags": ["a"], "opt": None})
    assert schema.validate({"amount": 12.5, "flag": False, "tags": ["b"], "opt": 3}) == {
        "amount": 12.5,
        "flag": False,
        "tags": ["b"],
        "opt": 3,
    }
    for bad in ({"new": 1}, {"amount": True}, {"note": 1}, {"flag": 1}):
        with pytest.raises(ValidationError):
            schema.validate(bad)


async def test_outcome_to_dict_is_json_serialisable():
    agent, _, _ = make_agent()
    outcome = await agent.submit(RISKY)
    encoded = json.loads(json.dumps(outcome.to_dict()))
    assert encoded["status"] == "pending_approval" and encoded["action"]["risk"] == "HIGH"
