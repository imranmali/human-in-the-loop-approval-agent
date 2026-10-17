# Spec Delta

## Purpose

Provides a tamper-evident, redacting and queryable audit trail of domain events (requests, approvals, rejections,
resumptions) with pluggable durable storage, so the history of any request or agent run can be reconstructed and trusted.

## ADDED Requirements

### Requirement: Hash-chained audit records
Each audit record SHALL carry a contiguous 0-based sequence number, a unique record id prefixed `aud`, kind, subject id,
actor, data, timestamp, optional correlation id, the hash of the previous record and its own SHA-256 hex hash computed
over the canonical JSON of all other fields including the previous hash. The first record SHALL link to a genesis hash
of 64 zeros.

#### Scenario: First record links to genesis
- **WHEN** the first event is recorded in an empty log
- **THEN** its sequence number is 0 and its previous hash is 64 zeros

#### Scenario: Records form a chain
- **WHEN** several events are recorded
- **THEN** sequence numbers are contiguous from 0 and each record's previous hash equals the prior record's hash

#### Scenario: Hash covers every field
- **WHEN** a record's hash is recomputed from its fields
- **THEN** it equals the stored hash, and changing any field changes the recomputed hash

#### Scenario: Record serialization round-trips
- **WHEN** a record is converted to a dictionary and back
- **THEN** the result equals the original record and a dictionary missing required fields is rejected with a validation error

### Requirement: Recording domain events
The audit log SHALL be usable as an event sink: recording an event stores its kind, subject id, actor and data. The
record timestamp SHALL be the event timestamp when positive, otherwise the injected clock's current time. A convenience
append SHALL build and record an event from kind, subject id, actor and data, rejecting invalid kinds.

#### Scenario: Event timestamp is preserved
- **WHEN** an event with a positive timestamp is recorded
- **THEN** the record carries that timestamp

#### Scenario: Missing timestamp uses the clock
- **WHEN** an event with timestamp 0 is recorded
- **THEN** the record carries the clock's current time

#### Scenario: Subscribed to an event bus
- **WHEN** the audit log is subscribed to an event bus and events are emitted
- **THEN** every emitted event is recorded in order

#### Scenario: Invalid kind is rejected
- **WHEN** a record is appended with a kind that is not a valid name
- **THEN** a validation error is raised and nothing is stored

### Requirement: Correlation ids
A record SHALL carry a correlation id taken, in order of precedence, from an explicit argument, the data key
`correlation_id`, or the data key `run_id`, converted to a string; otherwise it SHALL have none.

#### Scenario: Correlation from run id
- **WHEN** an event whose data contains `run_id` is recorded
- **THEN** the record's correlation id is that run id

#### Scenario: Explicit correlation key wins
- **WHEN** event data contains both `correlation_id` and `run_id`
- **THEN** the record's correlation id is the `correlation_id` value

### Requirement: Concurrent appends
Appends from multiple threads or asyncio tasks SHALL be serialized so that sequence numbers stay contiguous, no record
is lost and the resulting chain verifies.

#### Scenario: Threads append concurrently
- **WHEN** many threads append records at the same time
- **THEN** the log contains every record with sequence numbers 0..n-1 and verification succeeds

#### Scenario: Async tasks append concurrently
- **WHEN** many asyncio tasks record events, including from worker threads
- **THEN** the log contains every record and verification succeeds

### Requirement: Redaction of sensitive data
Before hashing and storage, the value of every data key matching a redact key (case-insensitive) SHALL be replaced by
the redaction marker at any nesting depth, including dictionaries inside lists. Defaults SHALL be password, secret,
token, api_key and ssn. Original values SHALL never be stored.

#### Scenario: Nested keys are redacted
- **WHEN** data contains `Password` at the top level and `api_key` inside a nested dictionary within a list
- **THEN** both values are stored as the redaction marker and other values are unchanged

#### Scenario: Originals never reach storage
- **WHEN** a record with a secret is written to a durable backend
- **THEN** the stored bytes do not contain the secret and the chain still verifies

#### Scenario: Custom redaction settings
- **WHEN** the log is created with custom redact keys and a custom marker
- **THEN** only those keys are redacted, with that marker

### Requirement: Chain verification
Verification SHALL recompute each record's hash and check its previous-hash link and contiguous sequence number over the
whole log or a range, returning a report of success, records checked, the first bad sequence position and an error
message. A raising variant SHALL raise an integrity error on failure.

#### Scenario: Intact log verifies
- **WHEN** an untampered log is verified
- **THEN** the report is ok, checked equals the number of records and no bad position is reported

#### Scenario: Edited record is detected
- **WHEN** the data of the record at position k is altered in storage
- **THEN** verification fails with first bad sequence k

#### Scenario: Deleted record is detected
- **WHEN** a record in the middle of the log at position k is removed from storage
- **THEN** verification fails with first bad sequence k

#### Scenario: Reordered records are detected
- **WHEN** the records at positions k and k+1 are swapped in storage
- **THEN** verification fails with first bad sequence k

#### Scenario: Forged hash is detected by the next link
- **WHEN** a record is altered and its own hash recomputed but its successor is untouched
- **THEN** verification fails at the successor's position

#### Scenario: Range verification
- **WHEN** a range of the log is verified
- **THEN** only that range is checked, linked to the hash of the record before the range

#### Scenario: Raising verification
- **WHEN** the raising variant runs on a tampered log
- **THEN** an integrity error naming the bad position is raised

### Requirement: External anchors
The log SHALL expose its head hash (the newest record's hash, or the genesis hash when empty) and SHALL verify that the
record at a given position has a given anchor hash and that the chain up to it is intact.

#### Scenario: Anchor matches
- **WHEN** the head hash is published and the log later grows
- **THEN** verifying against the published anchor at its position succeeds

#### Scenario: Truncation or rewrite is detected
- **WHEN** the log no longer contains the anchored position, or the record there has a different hash
- **THEN** verification against the anchor fails

### Requirement: Querying with pagination
The log SHALL return records ordered by sequence number filtered by any combination of subject id, kind (exact, or a
prefix when ending with `*`), actor, correlation id and time window (since inclusive, until exclusive), paginated by
limit and offset; negative limits or offsets SHALL be rejected. History SHALL return all records of a subject.

#### Scenario: Filter by kind prefix
- **WHEN** records of kinds `approval.requested`, `approval.granted` and `run.started` exist and kind `approval.*` is queried
- **THEN** only the two approval records are returned

#### Scenario: Combined filters
- **WHEN** subject, actor, correlation id and time window filters are combined
- **THEN** only records matching all filters are returned

#### Scenario: Pagination
- **WHEN** a query uses limit and offset
- **THEN** it returns the corresponding page in sequence order

#### Scenario: Subject history
- **WHEN** the history of a subject is requested
- **THEN** every record of that subject is returned in sequence order

#### Scenario: Invalid pagination
- **WHEN** a negative limit or offset is passed
- **THEN** a validation error is raised

### Requirement: Pluggable storage backends
Storage SHALL be pluggable through a protocol providing append, length, get by position, iteration from a position and
last record, with an in-memory default. Fetching a missing position SHALL raise a not-found error.

#### Scenario: In-memory default
- **WHEN** a log is created without a backend
- **THEN** it stores records in memory and satisfies the backend protocol

#### Scenario: Missing position
- **WHEN** a position beyond the end is fetched
- **THEN** a not-found error is raised

### Requirement: Durable JSON Lines storage
A JSON Lines backend SHALL append one canonical JSON record per line, optionally fsync each append, reload existing
records on open so the chain continues across restarts, rebuild query indexes, and stream records on iteration. A torn
or unparseable line SHALL raise an integrity error.

#### Scenario: Reopen continues the chain
- **WHEN** a log over a file is closed, reopened and appended to
- **THEN** sequence numbers continue, the new record links to the previous head and the whole chain verifies

#### Scenario: Indexes rebuilt on reopen
- **WHEN** a file-backed log is reopened
- **THEN** subject and correlation queries return the previously stored records

#### Scenario: Tampered line is detected
- **WHEN** one line of the file is rewritten with altered data and the log is reopened
- **THEN** verification fails at that line's position

#### Scenario: Torn trailing line
- **WHEN** the file ends with a partial line lacking a newline
- **THEN** opening it raises an integrity error

### Requirement: Export and import
The log SHALL export all records as JSON Lines to a path or writable stream, and SHALL import JSON Lines from a path or
iterable of lines into an in-memory log, verifying the chain by default and raising an integrity error on failure.

#### Scenario: Export and import round-trip
- **WHEN** a log is exported and the output imported
- **THEN** the imported log has identical records and the same head hash

#### Scenario: Import rejects tampered data
- **WHEN** an exported line is altered and imported with verification
- **THEN** an integrity error is raised

#### Scenario: Import without verification
- **WHEN** tampered lines are imported with verification disabled
- **THEN** the log is returned and its own verification reports the bad position
