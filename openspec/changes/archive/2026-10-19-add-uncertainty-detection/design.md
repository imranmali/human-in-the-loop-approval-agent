# Design

## Context

`hitl.core` provides the `Action` model (name, params, `RiskLevel`, self-reported `confidence`), `fingerprint`,
the error hierarchy, `Clock` and `EventSink`. Capability modules may depend only on `hitl.core` and communicate
through events. See proposal.md for motivation; requirements live in `specs/uncertainty-detection/spec.md`.

## Goals / Non-Goals

**Goals:**
- One small, synchronous decision call (`assess`) that is cheap enough to run before every action.
- Explainability: every decision carries per-signal scores and ordered, human-readable reasons.
- Extensibility via a structural `Signal` Protocol; no base class to inherit.
- Deterministic and testable: injected clock, ids from `new_id`, no I/O.

**Non-Goals:**
- Learning or calibrating thresholds from outcomes (callers can tune `threshold`/`weights`).
- Persisting novelty memory across processes (an in-memory, bounded default only).
- Routing the review itself (approval workflow is a separate capability that consumes the events).

## Decisions

- **Signals return `SignalResult(name, score, reason, forces_review)`.** Separating a continuous score from a
  boolean veto lets hard policy (deny lists, risk ceilings, confidence floors) bypass aggregation maths, while
  soft evidence still combines. Alternative: encode vetoes as score 1.0 — rejected because `weighted`
  aggregation could dilute a 1.0 below threshold.
- **Confidence signal uses `threshold` for explanation and `floor` for vetoes.** Score is always
  `1 - confidence`; `threshold` only decides whether the reason says "below threshold"; `floor` (optional)
  forces review. Keeps the signal simple and leaves the review decision to the detector threshold.
- **Risk mapping LOW 0.0 / MEDIUM 0.33 / HIGH 0.67 / CRITICAL 1.0**, overridable, plus `review_at` veto.
- **Aggregation strategies:**
  - `max` (default): `max(min(1, w*s))` — most conservative and order independent.
  - `weighted`: `sum(w*s) / sum(w)` over all signals (default weight 1.0; zero total weight → 0).
  - `noisy_or`: `1 - prod(1 - min(1, w*s))` — independent evidence accumulates.
  Weights default to 1.0, must be non-negative, and may name signals not (yet) registered.
  Alternative considered: a pluggable aggregator callable — deferred; three named strategies cover the cases
  and keep assessments reproducible from their serialised form.
- **Fail-safe signals.** Exceptions or non-`SignalResult` returns become `score=1.0, forces_review=True` with
  reason `signal <name> failed: <Type>: <msg>`, and the failure is logged. Safety beats availability for a
  gatekeeper.
- **Novelty memory is an `OrderedDict` LRU keyed by `fingerprint({name, sorted param keys})`**, bounded by
  `max_tracked`, guarded by a `threading.Lock`. Param *values* are excluded so "transfer 10" and "transfer 20"
  share a shape. Observation is explicit (`observe`) so callers decide what counts as "seen" (e.g. only
  approved actions); the detector offers `observe()` that forwards to every signal that has one.
- **Disagreement compares candidates by `fingerprint`**, so structurally equal dicts/lists agree regardless of
  key order. Missing key or fewer than two candidates → score 0; a non-list value raises `ValidationError`,
  which the fail-safe turns into a forced review (malformed context is itself suspicious).
- **Copy-on-write signal registry.** `add_signal`/`remove_signal` replace an immutable tuple, so an in-flight
  `assess` always sees a consistent snapshot without locking the hot path. `assess` is synchronous and never
  awaits, so it cannot interleave on the event loop; `assess_many` yields (`asyncio.sleep(0)`) between items so
  large batches do not starve other tasks.
- **Events.** `uncertainty.assessed` always, `uncertainty.review_required` additionally when review is
  required; `subject_id` is the assessment id and `timestamp` equals `created_at`. A raising sink is logged and
  isolated so auditing problems never turn a "review required" decision into an exception that a caller might
  swallow.

## Risks / Trade-offs

- [Novelty forgets shapes under heavy churn] → `max_tracked` is configurable; eviction only makes the detector
  *more* conservative (an evicted shape looks novel again).
- [`max` aggregation can be noisy with many weak signals] → callers can switch to `weighted` or tune weights.
- [`threshold=0` makes every action require review] → documented; it is a legitimate "always review" mode.
- [Isolating sink failures could hide audit outages] → failures are logged via the `hitl` logger.
- [Self-reported confidence may be miscalibrated] → combine with risk, rules and disagreement signals.
