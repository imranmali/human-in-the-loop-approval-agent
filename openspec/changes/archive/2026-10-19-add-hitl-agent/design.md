## Context

The four capability modules depend only on `hitl.core` and publish `Event`s. `HITLAgent` is the only place where they meet.

## Goals / Non-Goals

**Goals:** fail-closed execution of uncertain actions, approvals bound to the exact action, human modifications validated before use, one audit trail for the full run, and horizontally scalable resume.

**Non-Goals:** a UI or transport for approvers, distributed locking across processes (the stores' optimistic concurrency handles this), and retrying failed handlers.

## Decisions

- **Shared EventBus → AuditLog.** Every component gets the bus as its `event_sink`, and the audit log subscribes to it. Modules stay decoupled, and the trail sees every transition in order.
- **Checkpoint first, then request.** The checkpoint holds the action, context and assessment, and the approval request carries the `checkpoint_id`. Resuming needs only the request id, so a different worker process can pick the run up.
- **Binding.** On resume, the action fingerprint stored in the checkpoint must equal the fingerprint on the approval request. A mismatch raises `IntegrityError` and nothing is executed.
- **Modification schema.** If a handler declares no schema, one is derived from the action's params: each existing key is optional and keeps its type, and unknown keys are rejected. Humans can therefore tweak values but cannot inject new arguments.
- **Idempotency.** The approval idempotency key is `run_id:action_fingerprint`, so re-submitting the same action in the same run returns the existing pending request.
- **Fail closed.** If a handler raises, the outcome is `FAILED`, never retried silently. Rejected, expired and cancelled requests discard the checkpoint and never execute.

## Risks / Trade-offs

- With in-memory stores, resume only works within one process. Swapping in shared stores removes that limit, but that backend is out of scope here.
- A derived schema cannot express ranges. Handlers that need ranges should pass an explicit `InputSchema`.
