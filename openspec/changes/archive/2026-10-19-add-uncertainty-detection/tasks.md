# Tasks

## 1. Signal model

- [x] 1.1 Implement `SignalResult` (validated score) and the `Signal` Protocol in `src/hitl/uncertainty.py`; verify with signal-result tests in `tests/test_uncertainty.py`

## 2. Built-in signals

- [x] 2.1 Implement `ConfidenceSignal` (threshold reason, optional floor veto); verify confidence scenario tests pass
- [x] 2.2 Implement `RiskSignal` (level mapping, `review_at` veto); verify risk scenario tests pass
- [x] 2.3 Implement `RuleSignal` (predicate, score, force); verify matching, non-matching and soft-rule tests pass
- [x] 2.4 Implement `NoveltySignal` (shape fingerprint, `min_observations`, bounded LRU `max_tracked`); verify novelty tests including eviction pass
- [x] 2.5 Implement `DisagreementSignal` (fingerprint-compared candidates); verify unanimous, split, too-few and malformed tests pass

## 3. Detector

- [x] 3.1 Implement `UncertaintyDetector` configuration validation; verify invalid-configuration tests pass
- [x] 3.2 Implement `max`, `weighted` and `noisy_or` aggregation and the empty-detector case; verify aggregation tests pass
- [x] 3.3 Implement `Assessment` (decision, ordered reasons, `to_dict`) and fail-safe signal evaluation; verify decision, serialisation and fail-safe tests pass
- [x] 3.4 Implement `add_signal`/`remove_signal` and async `assess_many`; verify runtime-management and batch tests pass
- [x] 3.5 Publish `uncertainty.assessed` / `uncertainty.review_required` events with clock timestamps and isolate sink failures; verify event tests pass
- [x] 3.6 Implement `default_detector()`; verify the default-detector scenario test passes

## 4. Verification

- [x] 4.1 Run `python3 -m pytest -q tests/test_uncertainty.py`, `ruff check` and `ruff format --check` on owned files, and `openspec validate add-uncertainty-detection --strict --no-interactive`; verify all succeed
