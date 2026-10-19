# Proposal

## Why

When the agent is uncertain or an action is risky, execution must pause until one or more humans decide what
happens next. The library has core models and events but no way to ask humans for a decision, enforce who may
answer, collect several answers into a quorum, or handle requests nobody answers in time.

## What Changes

- Add an approval workflow that turns a proposed `Action` into an approval request with a policy (quorum, eligible
  roles, timeout, optional one-step escalation, self-approval and reject-comment rules).
- Collect human decisions (approve, reject, approve-with-modifications) with authorization checks, veto semantics
  for rejection and quorum semantics for approval.
- Expire or escalate requests that pass their deadline, both lazily (on decision) and through a batch sweep.
- Allow cancellation, awaiting a terminal outcome, and paginated listing of pending work per approver.
- Make request creation idempotent through a caller-provided key, and bind the reviewed context to the request with a
  fingerprint so tampering is detectable.
- Publish lifecycle events (`approval.*`) to an injected event sink so audit and orchestration can observe the
  workflow without importing it.
- Keep persistence and approver lookup pluggable (store and directory protocols with in-memory defaults).

## Capabilities

### New Capabilities

- `approval-workflow`: requesting, authorizing, collecting and resolving human decisions on proposed agent actions,
  including quorum, veto, modifications, expiry, escalation, cancellation, waiting and listing.

### Modified Capabilities

None.

## Impact

- New module `src/hitl/approvals.py` depending only on `hitl.core`; new tests `tests/test_approvals.py`.
- New public names to re-export from `hitl/__init__.py` (done by the package owner).
- No new runtime dependencies; no changes to existing behavior.
