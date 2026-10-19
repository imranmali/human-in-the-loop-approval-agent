## Why

Uncertainty detection, approval collection, checkpointing and auditing are useful alone, but a safe agent needs them wired into one guarded execution path: no high-uncertainty action may run without a recorded human decision, and the resumed run must use exactly the context the human validated.

## What Changes

- Add `HITLAgent` in `hitl/agent.py`, which composes the detector, approval service, pause/resume manager and audit log behind a shared `EventBus`.
- Add action handler registration with an optional per-action approval policy and a schema for the modifications a human may make.
- Add a non-blocking `submit` → `resume` flow, a blocking `run` convenience method, and a `process_decided` sweep. All run state lives in the pluggable stores, so any worker that shares those stores can resume a paused run.
- Add `Outcome` and `RunStatus`, which report how each run ended.

## Capabilities

### New Capabilities
- `hitl-agent`: the guarded execution loop: assess → (execute | pause → request approval → validate decision → resume or abort), with every step audited.

### Modified Capabilities

## Impact

- New module `src/hitl/agent.py`, plus exports in `src/hitl/__init__.py`.
- Depends on the `uncertainty-detection`, `approval-workflow`, `pause-resume` and `audit-trail` capabilities.
- No runtime dependencies.
