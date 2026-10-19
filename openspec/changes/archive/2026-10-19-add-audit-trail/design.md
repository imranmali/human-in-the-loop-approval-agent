# Design

## Context

See proposal.md for motivation. The audit trail lives in `src/hitl/audit.py` and may depend only on
`hitl.core` (errors, `Clock`, `new_id`, `canonical_json`, `Event`). Other modules never import it: they publish
`Event`s to an `EventSink`, and the composing agent subscribes the audit log to its `EventBus`. Behaviour is
specified in `specs/audit-trail/spec.md`.

## Goals / Non-Goals

**Goals:**

- Detect any in-place edit, deletion, insertion or reordering of stored records with a precise first bad position.
- Keep appends safe under concurrent threads and coroutines without an external lock.
- Keep memory bounded for durable logs: a byte-offset index plus small secondary indexes, never every record.
- Make storage swappable (SQL, object store, log service) by implementing a five-method protocol.

**Non-Goals:**

- Cryptographic signatures / non-repudiation (an HMAC or signing layer can wrap `head_hash` later).
- Multiple concurrent writer *processes* on one file; a log has a single writer process.
- Detecting truncation of the newest records without an external anchor (that is what anchors are for).
- Retention / compaction of old records.

## Decisions

1. **Hash chain over canonical JSON.** `hash = sha256(canonical_json({seq, record_id, kind, subject_id, actor,
   data, timestamp, prev_hash, correlation_id}))`; the first record links to `GENESIS_HASH = "0" * 64`.
   Including `seq` and `prev_hash` in the hashed payload makes deletion and reordering detectable, not just edits.
   *Alternative:* a Merkle tree gives O(log n) inclusion proofs but complicates appends and storage; a linear chain
   plus published anchors is sufficient for an approval log and trivially streamable.
2. **Normalise data to plain JSON before hashing.** Redacted data is round-tripped through `canonical_json` so
   enums, sets, tuples and other values become JSON-native. The hash computed at append time is therefore the
   same hash recomputed after a reload from JSON Lines. *Alternative:* hashing the raw objects would make the
   hash depend on Python types that do not survive persistence.
3. **Redact before hashing, never store originals.** Matching is case-insensitive on dictionary keys at any depth,
   including dictionaries inside lists. Because redaction precedes hashing, the chain verifies without the secret
   and the secret never reaches any backend. *Alternative:* hashing the original and storing a redacted copy would
   make verification impossible without the secret.
4. **Seq and prev_hash assigned under one `threading.Lock` in `AuditLog`.** The log caches the head hash so an
   append is O(1). Appends do no I/O awaits, so the same lock makes calls from asyncio tasks safe too.
   *Alternative:* optimistic compare-and-swap in the backend would allow multiple writers but needs backend support
   every implementation would have to get right.
5. **Dumb backends.** Backends only store, count, fetch by position and iterate; they do not validate the chain.
   Positions equal sequence numbers for an intact log, so a deleted or reordered record surfaces as a seq mismatch at
   a position during verification. This keeps third-party backends trivial and means verification trusts nothing
   the backend says.
6. **JSON Lines backend with a byte-offset index.** On open the file is scanned once to record the offset of every
   line and parse the last record (to continue the chain). `get` seeks to one offset; iteration opens its own handle
   and streams lines up to the count observed when iteration started, so concurrent appends do not disturb readers.
   Optional `fsync=True` trades latency for durability. A trailing line without a newline (torn write) or an
   unparseable line raises `IntegrityError` rather than being silently dropped.
7. **Secondary indexes in `AuditLog`.** `subject_id -> [seq]` and `correlation_id -> [seq]` are kept in memory and
   rebuilt by one streaming pass when a log opens over an existing backend. Queries filtered by either use the index
   (O(k) fetches); other filters stream the backend. Results are ordered by seq and paginated with limit/offset.
8. **Correlation id derivation.** An explicit `correlation_id` wins; otherwise the event data's `correlation_id`,
   then `run_id` (after redaction), converted to a string. This lets every record of one agent run be retrieved
   without callers changing their event payloads.
9. **Verification report instead of exceptions by default.** `verify()` returns
   `VerificationReport(ok, checked, first_bad_seq, error)` so monitoring can alert without try/except;
   `verify_or_raise()` raises `IntegrityError` for fail-closed callers.

## Risks / Trade-offs

- [Tail truncation is invisible to `verify()`] → publish `head_hash` periodically and check with `verify_against`.
- [Index memory grows with distinct subjects] → indexes hold ints only; a database backend can replace them.
- [An attacker who can rewrite the whole file can recompute every hash] → external anchors make such rewrites
  detectable; signing is a non-goal for this change.
- [`fsync=False` can lose the newest records on power loss] → opt-in `fsync=True`; torn tails fail loudly on reopen.
- [Two `AuditLog`s over one backend would fork the chain] → documented single-writer rule; verification detects it.

## Migration Plan

New module with no existing data; nothing to migrate. Rollback is removing the sink subscription.
