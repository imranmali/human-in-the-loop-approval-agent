# Tasks

## 1. Records and hash chain

- [x] 1.1 Implement `AuditRecord` (frozen; fields, `compute_hash`, `to_dict`/`from_dict`) and `GENESIS_HASH` in `src/hitl/audit.py`; verify with record round-trip, hash-coverage and from_dict validation tests in `tests/test_audit.py`
- [x] 1.2 Implement redaction (case-insensitive, nested dicts/lists, configurable keys and marker) and JSON normalisation before hashing; verify with nested, custom and durable-storage redaction tests

## 2. Storage backends

- [x] 2.1 Define the runtime-checkable `AuditBackend` protocol and `InMemoryAuditBackend` (thread-safe, NotFoundError on missing position); verify with protocol-conformance and missing-position tests
- [x] 2.2 Implement `JsonlAuditBackend` (append-only, optional fsync, byte-offset index, reload on open, streaming iteration, IntegrityError on torn/unparseable lines); verify with reopen, torn-tail and streaming tests

## 3. Audit log

- [x] 3.1 Implement `AuditLog.record` / `__call__` / `append` with lock-protected seq and prev_hash assignment, clock fallback for timestamps and correlation id derivation; verify with genesis, chain, timestamp, correlation, EventBus, threaded and asyncio tests
- [x] 3.2 Implement `verify`, `verify_or_raise`, `head_hash` and `verify_against`; verify with edit, delete, reorder, forged-hash, range, raising and anchor tests (in-memory and JSON Lines)
- [x] 3.3 Implement subject/correlation indexes (rebuilt on open), `query` with kind prefix, time window and limit/offset pagination, and `history`; verify with filter, pagination, invalid-pagination and reopen-index tests
- [x] 3.4 Implement `export_jsonl` and `import_jsonl(verify=True)`; verify with round-trip, tampered-import and unverified-import tests

## 4. Integration checks

- [x] 4.1 Run `python3 -m pytest -q tests/test_audit.py`, `ruff check` and `ruff format --check` on the owned files, and `openspec validate add-audit-trail --strict --no-interactive`; all pass
