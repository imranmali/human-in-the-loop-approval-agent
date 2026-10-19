# Proposal

## Why

A human-in-the-loop agent needs a principled, auditable way to decide *when* to stop and ask a human. Today
nothing in `hitl` turns an agent's proposed `Action` (its confidence, declared risk, novelty, policy rules,
disagreement between candidate outputs) into a single, explainable "pause for review" decision, so every
caller would hand-roll ad-hoc thresholds that are hard to test, tune or audit.

## What Changes

- Add a pluggable **signal** abstraction: a named evaluator that scores an action in [0, 1] with a
  human-readable reason and may *force* review regardless of score.
- Ship built-in signals: confidence (with a hard confidence floor), declared risk level, policy rules
  (arbitrary predicates), novelty (bounded LRU memory of seen action shapes) and candidate disagreement.
- Add an **uncertainty detector** that runs signals, aggregates their scores (`max`, `weighted` mean or
  `noisy_or`), compares to a threshold and returns an immutable, serialisable **assessment** that says whether
  review is required and why.
- Fail safe: a signal that raises or misbehaves forces review instead of silently passing.
- Signals can be added and removed at runtime; batches can be assessed asynchronously.
- Publish `uncertainty.assessed` and `uncertainty.review_required` events to an injected event sink, stamped
  by an injected clock.
- Provide a conservative `default_detector()` factory (confidence floor 0.5, review at HIGH risk, novelty).

## Capabilities

### New Capabilities
- `uncertainty-detection`: scoring a proposed action with pluggable signals and deciding, with reasons,
  whether the agent must pause for human review.

### Modified Capabilities
- None.

## Impact

- New module `src/hitl/uncertainty.py` depending only on `hitl.core`; new tests `tests/test_uncertainty.py`.
- New public names to re-export from `hitl/__init__.py` (done by the package owner, not this change).
- No new runtime dependencies; no breaking changes.
