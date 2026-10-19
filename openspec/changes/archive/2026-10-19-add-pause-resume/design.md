# Design

## Context

See proposal.md for motivation and specs/pause-resume/spec.md for the behavioral contract. The module must depend
only on `hitl.core` (errors, `Clock`, `new_id`, `fingerprint`, `canonical_json`, `Event`, `EventSink`), use the
standard library only, and be deterministic under `ManualClock`. Other modules (approval queue, audit trail, agent)
consume its events rather than importing it.

## Goals / Non-Goals

**Goals:**

- One module `hitl.checkpoint` exposing `FieldSpec`, `InputSchema`, `SchemaValidationError`, `MISSING`,
  `CheckpointStatus`, `Checkpoint`, `CheckpointPage`, `CheckpointStore`, `InMemoryCheckpointStore`,
  `ResumedContext` and `PauseResumeManager`.
- Correct behavior with many concurrent coroutines in one process, and safe (if less convenient) behavior with
  several manager instances sharing one store.

**Non-Goals:**

- Durable stores (SQL, Redis); only the Protocol and an in-memory implementation are provided.
- Cryptographic signing of checkpoints (HMAC with secret keys); the fingerprint is tamper-*evidence* against
  accidental or naive edits, the audit trail capability provides the stronger chain.
- Schema (de)serialization from dicts (`InputSchema.from_dict`); `to_dict` is descriptive only.

## Decisions

### State is normalized to plain JSON at pause time
A recursive normalizer accepts only dict (str keys), list/tuple, str, finite int/float, bool and None, producing a fresh
deep copy (tuples become lists). Errors carry the JSON path (e.g. `state.items[2]`).
*Alternatives:* `json.loads(json.dumps(x))` silently converts int keys to strings and accepts NaN; `copy.deepcopy`
accepts anything and the fingerprint (which uses `repr` as a fallback) would not be stable across processes.

### Fingerprint covers the state only and is compared in constant time
`state_fingerprint = core.fingerprint(state)`. On resume we recompute and compare with `hmac.compare_digest`, then
compare against the optional caller-supplied `expected_fingerprint` (the value the human UI displayed), which binds the
human's decision to the exact state they saw.
*Alternative:* fingerprint the whole checkpoint; rejected because status/version legitimately change.

### Merge semantics: `merged = {**state, **human_input}`
The cleaned human input overlays the state at the top level; the human wins on key collisions. The untouched pieces
are also exposed separately (`state`, `human_input`) so callers that need namespacing can do it themselves.
*Alternative:* nesting under `"human_input"`; rejected as it forces every consumer to know the convention while the
separate fields already give that view.

### Single-use, atomic resume: per-id `asyncio.Lock` plus optimistic versioning
Inside one manager, a per-checkpoint lock (kept in a `WeakValueDictionary`, so idle locks are garbage-collected)
serializes resume/discard/expire. Across managers sharing a store, `put(cp, expected_version)` performs
compare-and-swap; the loser gets `StateError`. Every transition increments `version`. `expected_version=None` means
"create" and fails if the id exists.
*Alternative:* a single global lock; simpler but serializes unrelated checkpoints.

### Validation failure does not consume the checkpoint
Input validation happens before any state change and raises `SchemaValidationError` (a `ValidationError` with
`errors: list[str]`). Checks run in this order: status, expiry, integrity, input. Integrity failure also leaves the
checkpoint untouched so an operator can investigate or discard it.

### Schema semantics
- Type check: `isinstance` against the declared type(s); `bool` only matches when `bool` (or `object`) is declared;
  `int` matches a declared `float`; `None` only matches when `type(None)` is declared.
- A type failure short-circuits other checks for that field; otherwise all constraint failures are collected.
- `min`/`max` compare numbers by value and `str`/`list`/`tuple`/`dict` by length.
- `pattern` uses `re.fullmatch` and applies to `str` values only.
- `validator(value)` returning `False` or a `str` is a failure (the string is the message); `True`/`None` pass; an
  exception is a failure whose message names the exception type.
- Field definitions are validated eagerly (including that a default satisfies the field).
- Error messages name the field and the constraint but never echo the submitted value, so they are safe to publish
  in events. Custom validator messages are the author's responsibility.
- Cleaned input is additionally normalized to JSON (same normalizer as state) so the merged context stays
  serializable.

### Pagination with a sequence cursor
The in-memory store assigns each inserted checkpoint a monotonically increasing sequence number; `list_active`
returns ACTIVE items with sequence greater than the cursor (`str(seq)` of the last item returned). Transitions never
change sequence numbers, so pages neither skip nor repeat remaining items. `limit` must be within 1..1000.

### Store isolation and test tampering
`InMemoryCheckpointStore` copies checkpoints on `put` and `get` (state/metadata deep-copied; the immutable schema is
shared). Its backing dict `_items` is documented as the supported way for tests to simulate tampering at rest
(`store._items[cid].state["amount"] = 1_000_000`), so no tamper API ships in production code.

### Events
Published to the injected sink (default `null_sink`) with the clock's time, subject = checkpoint id, actor = acting
human or `"system"`. Sink exceptions are caught and logged, mirroring `EventBus`. Kinds and data:
`checkpoint.paused` (run_id, step, state_fingerprint, expires_at, input_fields),
`checkpoint.resumed` (run_id, step, state_fingerprint, input_fingerprint, input_keys),
`checkpoint.resume_rejected` (run_id, step, reason, errors),
`checkpoint.discarded` (run_id, step, reason), `checkpoint.expired` (run_id, step, expires_at).

## Risks / Trade-offs

- [In-memory store loses checkpoints on restart] → Protocol allows a durable backend; documented as dev/test default.
- [Fingerprint is not a MAC; an attacker with store write access can rewrite state and fingerprint together] →
  Out of scope; pair with the audit trail (which records `state_fingerprint` at pause) or `expected_fingerprint`.
- [Lazy expiry: a checkpoint past its deadline stays ACTIVE in the store until resumed or `expire_due` runs] →
  `active_for_run` filters overdue checkpoints; callers schedule `expire_due` periodically.
- [Custom validator messages could echo secrets into events] → Documented; built-in messages never include values.
- [`expire_due` scans all active checkpoints] → Acceptable for in-memory; durable stores can index by `expires_at`.

## Migration Plan

Additive only: new module and tests; the package integrator re-exports the public names. Rollback = remove module.
