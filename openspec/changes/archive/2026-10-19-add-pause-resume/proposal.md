# Proposal

## Why

An agent that defers to a human must be able to stop at a well-defined step, persist exactly what it knew, and
later continue with the human's answer, without the human's input being malformed, the saved state being silently
altered, or the same checkpoint being resumed twice. Today `hitl` has no way to capture and safely restore that
context, so every integrator would reinvent it (usually without replay or tamper protection).

## What Changes

- Add a `pause-resume` capability (module `hitl.checkpoint`) that lets an agent run pause at a named step and
  persist a JSON-serializable snapshot of its state as a checkpoint with an optional expiry.
- Add declarative input schemas (typed fields with required/default/choices/range/length/pattern/custom validators)
  that validate a human's input, report every problem at once, and describe themselves for a review UI.
- Add a resume operation that is single-use (replay protected), atomic under concurrency, expiry-aware, checks the
  stored state against its fingerprint (tamper evidence) and optionally against a caller-supplied fingerprint, and
  returns the original state merged with the validated human input.
- Add discard, lookup, per-run listing, paginated listing of active checkpoints, and bulk expiry of overdue checkpoints.
- Add a pluggable asynchronous checkpoint store with optimistic concurrency control and an in-memory default.
- Publish domain events (`checkpoint.paused`, `checkpoint.resumed`, `checkpoint.resume_rejected`,
  `checkpoint.discarded`, `checkpoint.expired`) that never contain raw human input values.

## Capabilities

### New Capabilities

- `pause-resume`: pausing an agent run at a checkpoint with its state, validating human input against a declared
  schema, and resuming exactly once with integrity, expiry and concurrency guarantees.

### Modified Capabilities

None.

## Impact

- New module `src/hitl/checkpoint.py` depending only on `hitl.core`; new tests `tests/test_checkpoint.py`.
- New public names to re-export from `hitl/__init__.py` (done by the package integrator, not this change).
- No new runtime dependencies; no breaking changes.
