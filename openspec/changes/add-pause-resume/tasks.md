# Tasks

## 1. Input schema

- [x] 1.1 Implement `MISSING`, `SchemaValidationError(errors)` and `FieldSpec` (types incl. bool≠int and int→float, required/default, choices, min/max by value or length, full-match pattern, custom validator, eager definition checks); verify with `pytest tests/test_checkpoint.py -k "field or bool or int_satisfies or none_requires or choices or range or pattern or validator or missing_sentinel"`
- [x] 1.2 Implement `InputSchema.validate` (cleaned deep copy, defaults, optional omission, extra-field policy, all errors at once, value-free messages) and `to_dict`; verify with `pytest tests/test_checkpoint.py -k "schema or required or default or optional or unknown_key or all_errors or non_mapping or echo"`

## 2. Checkpoint model and store

- [x] 2.1 Implement `CheckpointStatus`, `Checkpoint` (copy, `is_overdue`, `to_dict`), `CheckpointPage` and `ResumedContext`; verify with `pytest tests/test_checkpoint.py -k "to_dict"`
- [x] 2.2 Implement the `CheckpointStore` Protocol and `InMemoryCheckpointStore` (copy-on-read/write, create vs. versioned compare-and-swap `put`, `list_by_run`, sequence-cursor `list_active` with limit/cursor validation, documented `_items` for tamper tests); verify with `pytest tests/test_checkpoint.py -k "store or listing or cursor or page"`

## 3. Pause / resume manager

- [x] 3.1 Implement `PauseResumeManager.pause` (argument checks, JSON normalization with paths, ttl/default_ttl, fingerprint, `checkpoint.paused` event); verify with `pytest tests/test_checkpoint.py -k "pause or ttl or serializable or tuples or caller_mutation"`
- [x] 3.2 Implement `resume` (per-id lock, not-found, state/expired checks with lazy EXPIRED marking, constant-time fingerprint and expected-fingerprint checks, input validation keeping ACTIVE on failure, versioned transition, merged context, value-free `checkpoint.resumed` / `checkpoint.resume_rejected` events, sink isolation); verify with `pytest tests/test_checkpoint.py -k "resume or tamper or fingerprint or concurrent or managers or sink or event"`
- [x] 3.3 Implement `discard`, `get`, `expire_due` (paged scan, re-check under lock, conflict-tolerant) and `active_for_run`; verify with `pytest tests/test_checkpoint.py -k "discard or unknown or expire or active_for_run"`

## 4. Integration checks

- [x] 4.1 Every spec scenario maps to at least one test and the whole file passes: `python3 -m pytest -q tests/test_checkpoint.py`
- [x] 4.2 Lint and format clean: `ruff check src/hitl/checkpoint.py tests/test_checkpoint.py` and `ruff format --check src/hitl/checkpoint.py tests/test_checkpoint.py`
- [x] 4.3 Change validates: `openspec validate add-pause-resume --strict --no-interactive`

## Workflow follow-up

- Re-export public names from `hitl/__init__.py` (owned by the package integrator).
- Archive the change after integration (`openspec archive add-pause-resume`).
