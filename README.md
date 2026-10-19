# Human-in-the-Loop Approval Agent

[![CI](https://github.com/imranmali/human-in-the-loop-approval-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/imranmali/human-in-the-loop-approval-agent/actions/workflows/ci.yml)

A dependency-free Python library for building **safe, compliant AI agents**. Before acting, the agent assesses each action for uncertainty. When it is unsure or the stakes are high, it **pauses**, asks a human to approve, reject or adjust the action, then **resumes with the validated context**. Every step is recorded in a **tamper-evident audit trail**.

```
            ┌────────────────────────┐
 action ──▶ │  UncertaintyDetector   │── confident ──────────────────────────────▶ execute ─┐
            └──────────┬─────────────┘                                                      │
                       │ uncertain / high risk                                              │
                       ▼                                                                    │
            ┌────────────────────────┐   ┌────────────────────────┐                         │
            │  PauseResumeManager    │──▶│   ApprovalService      │◀── human: approve /     │
            │  checkpoint run state  │   │   quorum · roles ·     │    reject / modify      │
            └──────────┬─────────────┘   │   escalation · expiry  │                         │
                       │                 └──────────┬─────────────┘                         │
                       │◀───── terminal decision ───┘                                       │
                       ▼                                                                    │
            validate modifications ─▶ verify binding ─▶ resume once ─▶ execute ─────────────┤
                                                                                            ▼
            ┌───────────────────────────────────────────────────────────────────────────────┐
            │  EventBus ─▶ AuditLog  (SHA-256 hash chain · redaction · JSONL · verification)│
            └───────────────────────────────────────────────────────────────────────────────┘
```

## Why it is safe

| Guarantee | How |
|---|---|
| **Fail closed** | If a detection signal crashes, the action goes to review. Rejected, expired and cancelled runs never execute. A failing handler becomes a `FAILED` outcome and is never retried silently. |
| **Approval is bound to the exact action** | The approval request and the checkpoint both carry the action's SHA-256 fingerprint. On resume, a mismatch raises `IntegrityError` and nothing runs. |
| **Validated human input** | Changes an approver makes are checked against an `InputSchema`. By default the schema comes from the action's params, so approvers can change existing values but cannot add new arguments. Invalid input leaves the run resumable. |
| **Exactly-once resume** | Checkpoints are single-use and store a version number. If several workers resume the same run at once, one wins and the rest get `StateError`. |
| **Separation of duties** | Approvers are authorized by role, requesters cannot approve their own requests by default, the quorum is configurable, and any single rejection is a veto. Comments are required on rejection. |
| **Tamper-evident audit trail** | Each record includes the hash of the previous one. `verify()` names the first altered, deleted or reordered record. Publish `head_hash` somewhere external to also catch records cut from the end. |
| **Privacy** | Sensitive keys such as `password`, `token` and `api_key` are redacted before hashing. Raw human input never appears in events; only its fingerprint and keys do. |

## Install & test

```bash
pip install -e '.[dev]'
python -m pytest -q                      # 354 tests
ruff check . && ruff format --check .
python examples/refund_approval.py       # end-to-end demo
```

Requires Python 3.11+. The runtime uses only the standard library.

## Quick start

```python
import asyncio
from hitl import (Action, ApprovalPolicy, Approver, HITLAgent, InMemoryApproverDirectory,
                  RiskLevel, RuleSignal, default_detector)

async def refund(action, context):
    return f"refunded {action.params['amount']}"

async def main():
    detector = default_detector(extra_signals=[
        RuleSignal("large-amount", lambda a, ctx: a.params.get("amount", 0) > 1_000,
                   "refund above 1,000 requires review"),
    ])
    agent = HITLAgent(
        detector=detector,
        directory=InMemoryApproverDirectory([Approver("alice", {"finance"})]),
        default_policy=ApprovalPolicy(required_roles={"finance"}, timeout_seconds=3600),
    )
    agent.register("payments.refund", refund)

    outcome = await agent.submit(
        Action("payments.refund", {"amount": 4_800}, risk=RiskLevel.HIGH, confidence=0.9))
    print(outcome.status)                      # RunStatus.PENDING_APPROVAL

    # ...later, from an approval UI, possibly in another process sharing the stores:
    await agent.approvals.decide(outcome.request_id, "alice", "modify",
                                 comment="partial refund", modifications={"amount": 2_400})
    final = await agent.resume(outcome.request_id, actor="alice")
    print(final.status, final.result)          # RunStatus.COMPLETED refunded 2400

    for record in agent.history(outcome.run_id):
        print(record.seq, record.kind, record.actor)
    assert agent.audit.verify().ok

asyncio.run(main())
```

Output of `examples/refund_approval.py`:

```
small refund -> refunded 40 EUR to c-1
large refund -> pending_approval | reasons: ['refund above 1,000 requires review', 'confidence 0.90']
after approval -> completed | refunded 2400 EUR to c-2

audit trail for run_9435f0f6...
  #3   agent.submitted              actor=agent
  #4   uncertainty.assessed         actor=system
  #5   uncertainty.review_required  actor=system
  #6   checkpoint.paused            actor=system
  #7   approval.requested           actor=agent
  #8   agent.paused                 actor=agent
  #9   approval.decided             actor=alice
  #10  approval.approved            actor=alice
  #11  checkpoint.resumed           actor=alice
  #12  agent.executed               actor=system
chain verified: True
```

## Components

The library is split into five modules. Each depends only on `hitl.core` and publishes `Event`s rather than calling the others; `HITLAgent` wires them together.

### 1. Uncertainty detection — `hitl.uncertainty`

Signals score an `Action` between 0 and 1. The detector combines the scores and decides whether the action needs review.

| Signal | Scores | Forces review when |
|---|---|---|
| `ConfidenceSignal(threshold=0.7, floor=None)` | `1 - confidence` | confidence < `floor` |
| `RiskSignal(review_at=RiskLevel.HIGH)` | LOW 0 · MEDIUM .33 · HIGH .67 · CRITICAL 1 | risk ≥ `review_at` |
| `RuleSignal(name, predicate, reason, score=1.0, force=True)` | `score` if the predicate matches | the predicate matches and `force` is true |
| `RuleSignal.always_review({"db.drop"})` | 1.0 for the listed actions | always |
| `NoveltySignal(max_tracked=10_000)` | 1.0 for an action shape it has not seen (bounded LRU memory) | — |
| `DisagreementSignal(key="candidates")` | `1 - share of the most common candidate` in `context[key]` | — |

```python
detector = UncertaintyDetector(signals, threshold=0.5, aggregation="max" | "weighted" | "noisy_or",
                               weights={"risk": 2.0})
assessment = detector.assess(action, context)   # Assessment(score, requires_review, reasons, ...)
await detector.assess_many([action_a, (action_b, ctx_b)])
```

Review is required when `score >= threshold` or when any signal forces it. A signal that raises an error is recorded as score 1.0 and forces review. `HITLAgent` calls `detector.observe(action)` after each successful execution, so an approved action shape stops counting as novel.

### 2. Approval workflow — `hitl.approvals`

```python
policy = ApprovalPolicy(
    required_approvals=2,                 # quorum
    required_roles={"finance"},           # approver must hold any of these roles
    timeout_seconds=3600,                 # deadline
    escalation_roles={"finance-lead"},    # on timeout: escalate once, then expire
    allow_self_approval=False,
    require_comment_on_reject=True,
)
service = ApprovalService(store, directory, clock, event_sink)
request = await service.request(action, "agent", reason="...", policy=policy, idempotency_key="run-1:...")
await service.decide(request.id, "alice", Verdict.APPROVE)               # or "approve"
await service.decide(request.id, "bob", "modify", modifications={"amount": 100})
await service.decide(request.id, "carol", "reject", comment="duplicate")  # veto
await service.wait(request.id, timeout=30)                                # await a terminal status
await service.pending_for("alice", limit=50, cursor=None)                 # an approver's inbox (paged)
await service.expire_due()                                                # escalation / expiry sweep
```

Statuses: `PENDING → APPROVED | REJECTED | EXPIRED | CANCELLED`. Decisions on one request are serialized by a per-request lock. A request's fingerprint covers its action and context, and is checked before every decision.

### 3. Pause & resume with validated context — `hitl.checkpoint`

```python
schema = InputSchema({
    "amount": FieldSpec(int, required=False, min=1, max=1_000),
    "reason": FieldSpec(str, choices={"goodwill", "defect"}),
    "email":  FieldSpec(str, pattern=r"[^@]+@[^@]+", required=False),
})
manager = PauseResumeManager(store, clock, event_sink, default_ttl=86_400)
checkpoint = await manager.pause(run_id, "refund", state, input_schema=schema)
ctx = await manager.resume(checkpoint.id, {"amount": 50, "reason": "defect"}, actor="alice")
ctx.merged        # {**state, **validated_input}
```

`resume` refuses with:
- `ExpiredError` if the checkpoint is past its deadline
- `StateError` if it was already resumed or discarded
- `IntegrityError` if the stored state no longer matches its fingerprint
- `SchemaValidationError` (with an `.errors` list) if the input is invalid; the checkpoint stays resumable

### 4. Audit trail — `hitl.audit`

```python
audit = AuditLog(JsonlAuditBackend("audit.jsonl"))   # or InMemoryAuditBackend (default)
bus.subscribe(audit)                                 # an AuditLog is also an EventSink
audit.query(kind="approval.*", actor="alice", since=t0, limit=100, offset=0)
audit.query(correlation_id=run_id)                   # everything that happened in one run
report = audit.verify()                              # VerificationReport(ok, checked, first_bad_seq, error)
anchor = audit.head_hash                             # publish externally
audit.verify_against(anchor, seq)                    # also detects truncation
audit.export_jsonl("export.jsonl"); AuditLog.import_jsonl("export.jsonl")   # import verifies the chain
```

The JSONL backend is append-only, can fsync each write, and continues the hash chain after a restart. When iterating it streams from disk and keeps only an index of byte offsets in memory.

### 5. The agent — `hitl.agent`

| Method | Purpose |
|---|---|
| `register(name, handler, modification_schema=None, policy=None)` / `@agent.action(name)` | Register a handler (sync or async) and, optionally, review rules for that action. |
| `await submit(action, run_id=None, context=None)` | Assess the action. Execute it, or pause it and return a `PENDING_APPROVAL` outcome. Resubmitting the same action in the same run returns the same request. |
| `await resume(request_id, actor=...)` | Continue a run whose request is decided: execute it (with validated modifications) or abort it. |
| `await run(action, timeout=...)` | Blocking convenience: submit, wait for the decision, resume. |
| `await process_decided(batch_size=100)` | Worker sweep: expire overdue requests and resume every decided run. |
| `history(run_id)` | The run's audit records, in order. |

An `Outcome` holds `status` (`COMPLETED`, `PENDING_APPROVAL`, `REJECTED`, `EXPIRED`, `CANCELLED` or `FAILED`), `result`, `error`, `assessment`, `request_id`, `checkpoint_id` and `modifications`.

## Scaling

- **Stateless workers.** Run state lives in the approval and checkpoint stores, not in the agent. Any worker connected to the same stores can resume a run that another worker paused (`test_another_worker_sharing_stores_can_resume`).
- **Pluggable storage.** `ApprovalStore`, `CheckpointStore`, `AuditBackend` and `ApproverDirectory` are Protocols. The in-memory defaults can be swapped for a database or queue without touching the core logic. The checkpoint store's version check is the hook for multi-process safety.
- **Paged everything.** Listing, inboxes, expiry sweeps and `process_decided` page through results with stable cursors, so none of them loads every row at once.
- **Bounded memory.** Novelty tracking and the run-correlation map are bounded LRUs, and locks are held weakly.
- **Async.** All of the I/O-shaped API is `async`. Tests cover concurrent submits, decisions and resumes run through `asyncio.gather`, as well as appends from several threads.

## Events

| Kind | Emitted when |
|---|---|
| `agent.submitted` · `agent.paused` · `agent.executed` · `agent.failed` · `agent.rejected` · `agent.expired` · `agent.cancelled` · `agent.integrity_failed` | run lifecycle |
| `uncertainty.assessed` · `uncertainty.review_required` | every assessment |
| `approval.requested` · `approval.decided` · `approval.approved` · `approval.rejected` · `approval.escalated` · `approval.expired` · `approval.cancelled` | approval lifecycle |
| `checkpoint.paused` · `checkpoint.resumed` · `checkpoint.resume_rejected` · `checkpoint.discarded` · `checkpoint.expired` | checkpoint lifecycle |

Every event the agent touches carries `run_id`, which the audit log indexes as the correlation id.

## Project layout

```
src/hitl/
  core.py          errors, Clock, Action, Event, EventBus, fingerprint
  uncertainty.py   signals + UncertaintyDetector
  approvals.py     ApprovalService, policies, stores, directory
  checkpoint.py    InputSchema, PauseResumeManager, checkpoint stores
  audit.py         AuditLog, in-memory and JSONL backends
  agent.py         HITLAgent
tests/             354 deterministic tests (ManualClock, no network)
examples/          refund_approval.py
openspec/specs/    the specifications this code implements
```

## Spec-driven development

The project was built spec-first with [OpenSpec](https://github.com/Fission-AI/OpenSpec). Each capability (`uncertainty-detection`, `approval-workflow`, `pause-resume`, `audit-trail`, `hitl-agent`) has a spec of requirements and scenarios in [`openspec/specs`](openspec/specs), and every scenario is covered by at least one test. Completed changes, each with its proposal, design and tasks, are kept under `openspec/changes/archive`.

```bash
npm install -g @fission-ai/openspec
openspec list --specs
openspec validate --all --strict
```

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs these jobs on every push and pull request:

- **lint**: `ruff check` and `ruff format --check`
- **test**: pytest on Python 3.11, 3.12 and 3.13, plus the end-to-end example
- **specs**: `openspec validate --all --strict`
