# Design

## Context

`hitl.core` provides the shared vocabulary (`Action`, `Event`, `EventSink`, `Clock`, `new_id`, `fingerprint`, error
hierarchy). Capability modules must depend only on `hitl.core` and communicate through events, so the approval
workflow cannot import the audit or pause/resume modules; the top-level agent composes them. See proposal.md for
motivation and specs/approval-workflow/spec.md for the behavior contract.

## Goals / Non-Goals

**Goals:**

- A single async service (`ApprovalService`) that owns all state transitions of an approval request.
- Pluggable persistence (`ApprovalStore`) and approver lookup (`ApproverDirectory`) so the in-memory defaults can be
  replaced by a database and an identity provider without changing the service.
- Deterministic behavior under an injected clock; correct behavior under concurrent decisions in one event loop.

**Non-Goals:**

- Notifying humans (email, chat): consumers react to `approval.requested` / `approval.escalated` events.
- Cross-process locking: a distributed backend must provide its own compare-and-set; the service lock is per process.
- A background scheduler: callers (or the agent) invoke `expire_due()` periodically.

## Decisions

1. **Rejection is a veto, approval is a quorum.** One REJECT resolves the request immediately; APPROVE and MODIFY
   both count toward `required_approvals`. Alternative (majority voting) was rejected: for safety-critical actions
   any informed objection must stop execution.
2. **MODIFY is an approval with parameter overrides.** Overrides from all MODIFY decisions are merged in decision
   order (later wins) into `effective_modifications`; `effective_action()` applies them to the action params. A
   separate "counter-proposal" round was considered and rejected as too heavy for the common case.
3. **Expiry is evaluated lazily and by sweep.** `decide()`/`cancel()` first apply any overdue timeout so a late
   decision can never sneak in; `expire_due()` sweeps pending requests via store paging in fixed-size batches so
   it scales with the pending set, not the total history. A timer per request was rejected (does not survive
   restarts, not deterministic).
4. **Single-step escalation.** If `escalation_roles` is set, the first timeout escalates (eligible roles switch to
   `escalation_roles`, `escalation_level` becomes 1, deadline becomes escalation time + `timeout_seconds` so the
   new approvers get a full window); the next timeout expires. Decisions already made keep counting toward quorum.
5. **Per-request `asyncio.Lock` held in a `WeakValueDictionary`.** Serializes read-modify-write of one request
   without a global lock and without leaking a lock per historical request. Idempotent creation uses the same
   mechanism keyed by idempotency key.
6. **Store copies on read and write.** The in-memory store deep-copies requests so callers cannot mutate stored
   state by accident, mirroring what a serializing backend does. `to_dict()`/`from_dict()` define the wire shape.
7. **Status index with monotonic sequence numbers.** The in-memory store keeps a sorted sequence list per status;
   the opaque cursor is the sequence number of the last returned item, so paging is stable while items change
   status (e.g. during an expiry sweep) and listing pending requests is O(log n + page).
8. **Context fingerprint.** `context_fingerprint = fingerprint({"action": action, "context": context})` is computed
   at creation and verified before any decision is recorded; a mismatch raises `IntegrityError`.
9. **Events after persistence, failures isolated.** Events are emitted after the store write with timestamps from
   the injected clock; a raising sink is logged and ignored so it cannot desynchronize stored state and callers.
10. **Waiting uses futures.** `wait()` registers a future before reading the store (no lost wake-up) and is resolved
    on any terminal transition; it raises `TimeoutError` (alias of `asyncio.TimeoutError`) on timeout.

## Risks / Trade-offs

- [Process-local locks] → A multi-process deployment needs a store with optimistic concurrency; documented on the
  `ApprovalStore` protocol.
- [Deep copies cost CPU for large contexts] → Acceptable for the in-memory default; real backends serialize anyway.
- [Lazy expiry requires a caller-driven sweep] → `expire_due()` is cheap and batchable; the agent can run it on a
  schedule.
- [`pending_for` filters after fetching] → It pages the pending index only and fetches no more than the remaining
  page size per batch, so it never skips items; highly selective filters may need a role index in a real backend.
