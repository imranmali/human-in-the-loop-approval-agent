# Proposal

## Why

Human-in-the-loop approvals are only trustworthy if there is an after-the-fact record of who requested,
approved, rejected or resumed what, and when. That record must be tamper-evident (edits, deletions and
reordering are detectable), must never retain secrets, and must stay queryable as it grows, so operators
and auditors can reconstruct the history of any request or agent run.

## What Changes

- Add a hash-chained, append-only audit trail: every record carries a contiguous sequence number, the hash
  of its predecessor and its own SHA-256 hash over a canonical encoding of all of its fields.
- Record core `Event`s directly: the audit log is usable as an `EventSink`, so any module (or an `EventBus`)
  can feed it without importing it.
- Redact sensitive keys (password, secret, token, api_key, ssn by default; case-insensitive; nested) before
  hashing and storage, so originals are never persisted.
- Verify the chain (whole log or a range), producing a report that pinpoints the first bad record, or raise
  `IntegrityError`; verify against an externally published head-hash anchor.
- Query by subject, kind (exact or `prefix.*`), actor, correlation id and time window with limit/offset
  pagination; per-subject history.
- Pluggable storage backends behind a protocol: an in-memory default and an append-only JSON Lines file
  backend that survives restarts and streams records instead of holding them all in memory.
- JSON Lines export and verified import.

## Capabilities

### New Capabilities

- `audit-trail`: tamper-evident, redacting, queryable record of domain events with pluggable storage.

### Modified Capabilities

(none)

## Impact

- New module `src/hitl/audit.py` (depends only on `hitl.core`) and tests in `tests/test_audit.py`.
- New public names to re-export from `hitl/__init__.py`: `AuditRecord`, `AuditBackend`,
  `InMemoryAuditBackend`, `JsonlAuditBackend`, `AuditLog`, `VerificationReport`, `GENESIS_HASH`.
- No new runtime dependencies (standard library only).
