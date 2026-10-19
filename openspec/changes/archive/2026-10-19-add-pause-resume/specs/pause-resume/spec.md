# Spec Delta

## Purpose

Lets an agent run pause at a named step, persist a tamper-evident snapshot of its state, and resume exactly once
with human input that has been validated against a declared schema.

## ADDED Requirements

### Requirement: Pausing creates an active checkpoint
The system SHALL let a caller pause a run at a named step with a state mapping, producing a checkpoint that has a
unique identifier, status ACTIVE, version 1, the creation time from the injected clock, and a SHA-256 fingerprint of
the canonical JSON encoding of the stored state. The run identifier and step MUST be non-empty strings.

#### Scenario: Pause stores an active checkpoint
- **WHEN** a run `run-1` is paused at step `review` with state `{"amount": 10}`
- **THEN** the returned checkpoint has an id prefixed `ckpt_`, status ACTIVE, version 1, the clock's current time as
  its creation time, and a state fingerprint equal to the fingerprint of `{"amount": 10}`
- **AND** fetching the checkpoint by id returns the same data

#### Scenario: Pause with an empty run id or step is rejected
- **WHEN** a pause is requested with an empty run id or an empty step
- **THEN** the system raises a validation error and stores nothing

#### Scenario: Pause publishes a paused event
- **WHEN** a run is paused
- **THEN** a `checkpoint.paused` event is published with the checkpoint id as subject, the clock's time as timestamp,
  and data containing the run id, the step and the state fingerprint

### Requirement: Checkpoints may expire after a time-to-live
The system SHALL set a checkpoint's expiry to its creation time plus the per-call time-to-live, falling back to the
manager's default time-to-live, and SHALL leave the expiry empty (never expires) when neither is given. A
time-to-live MUST be a positive number.

#### Scenario: Explicit time-to-live sets the expiry
- **WHEN** a run is paused at time 100 with a time-to-live of 60
- **THEN** the checkpoint expires at 160

#### Scenario: Default time-to-live applies
- **WHEN** the manager has a default time-to-live of 30 and a run is paused at time 0 without a time-to-live
- **THEN** the checkpoint expires at 30

#### Scenario: No time-to-live means no expiry
- **WHEN** a run is paused without a time-to-live and the manager has no default
- **THEN** the checkpoint has no expiry

#### Scenario: Non-positive time-to-live is rejected
- **WHEN** a run is paused with a time-to-live of 0 or a negative number
- **THEN** the system raises a validation error

### Requirement: Checkpoint state is JSON-serializable and isolated
The system SHALL accept only state made of JSON types (objects with string keys, arrays, strings, finite numbers,
booleans, null), normalizing tuples to arrays, and SHALL store an independent deep copy so later mutation of the
caller's objects or of returned checkpoints cannot change the stored state.

#### Scenario: Non-serializable state is rejected at pause
- **WHEN** a run is paused with state containing a set, an arbitrary object, a non-string key or a NaN number
- **THEN** the system raises a validation error naming the offending path and stores nothing

#### Scenario: Caller mutation after pause does not affect the checkpoint
- **WHEN** the caller mutates the state object it passed to pause, or mutates a checkpoint returned by the system
- **THEN** the stored state and its fingerprint are unchanged and a later resume succeeds

### Requirement: Input schema validates typed fields
The system SHALL validate a human input mapping against an input schema of named fields, each declaring accepted
types, and SHALL return a cleaned copy. A boolean MUST NOT satisfy an integer or float type unless bool is declared;
an integer SHALL satisfy a float type. None is accepted only when NoneType is declared.

#### Scenario: Correct types pass
- **WHEN** input `{"amount": 5, "note": "ok"}` is validated against fields `amount: int` and `note: str`
- **THEN** the cleaned result equals the input

#### Scenario: Boolean is not an integer
- **WHEN** input `{"amount": true}` is validated against field `amount: int`
- **THEN** validation fails with an error for `amount`

#### Scenario: Integer satisfies float
- **WHEN** input `{"ratio": 1}` is validated against field `ratio: float`
- **THEN** validation succeeds

### Requirement: Input schema handles required, optional and default fields
The system SHALL report a missing required field without a default, SHALL fill a missing field that has a default
with a copy of that default, and SHALL omit a missing optional field without a default from the cleaned result.

#### Scenario: Missing required field
- **WHEN** a required field without a default is absent from the input
- **THEN** validation fails with a "required" error for that field

#### Scenario: Default is applied
- **WHEN** a field with default `"low"` is absent from the input
- **THEN** the cleaned result contains that field with value `"low"`

#### Scenario: Optional field omitted
- **WHEN** an optional field without a default is absent from the input
- **THEN** the cleaned result does not contain that field

### Requirement: Input schema enforces constraints
The system SHALL enforce, per field, an allowed set of choices, a minimum and maximum (compared to the value for
numbers and to the length for strings, lists and mappings), a full-match regular expression for strings, and a custom
validator whose result False or a string message marks the value invalid; a raising validator also marks it invalid.

#### Scenario: Value outside choices
- **WHEN** a field allows choices `approve` and `reject` and the input value is `maybe`
- **THEN** validation fails with an error for that field

#### Scenario: Numeric range
- **WHEN** a numeric field has minimum 1 and maximum 100 and the input value is 0 or 101
- **THEN** validation fails with an error for that field

#### Scenario: Length range
- **WHEN** a string or list field has a maximum of 3 and the input value has length 4
- **THEN** validation fails with a length error for that field

#### Scenario: Pattern mismatch
- **WHEN** a string field has pattern `[A-Z]{3}` and the input value is `abcd`
- **THEN** validation fails with an error for that field

#### Scenario: Custom validator message
- **WHEN** a field's custom validator returns the message `must be even` for the input value
- **THEN** validation fails with an error for that field containing `must be even`

#### Scenario: Custom validator returns False or raises
- **WHEN** a field's custom validator returns False or raises an exception
- **THEN** validation fails with an error for that field

### Requirement: Input schema controls extra fields and reports every problem
The system SHALL reject input keys not declared in the schema unless the schema allows extra fields, in which case
they are passed through. A failed validation MUST raise a schema validation error, a kind of validation error, whose
error list contains every problem found rather than only the first.

#### Scenario: Unknown key rejected
- **WHEN** input contains a key not declared by a schema that does not allow extra fields
- **THEN** validation fails with an error naming that key

#### Scenario: Unknown key allowed
- **WHEN** input contains an undeclared key and the schema allows extra fields
- **THEN** the key and its value are present in the cleaned result

#### Scenario: All errors reported at once
- **WHEN** input has a wrong type in one field, a missing required field and an unknown key
- **THEN** the raised error lists all three problems

#### Scenario: Non-mapping input rejected
- **WHEN** the input is not a mapping
- **THEN** validation fails with a schema validation error

### Requirement: Malformed schema definitions are rejected
The system SHALL reject a field definition whose type is not a type or tuple of types, whose minimum exceeds its
maximum, whose pattern is not a valid regular expression, whose choices are a plain string, whose validator is not
callable, or whose default does not itself satisfy the field.

#### Scenario: Invalid field definitions
- **WHEN** a field is defined with minimum 5 and maximum 1, or with pattern `(`, or with an int default `"x"`
- **THEN** defining the field raises a validation error

### Requirement: Input schema describes itself for a review UI
The system SHALL describe an input schema as a JSON-serializable mapping listing, per field, the type names,
whether it is required, its default, choices, minimum, maximum, pattern, description and whether a custom validator is
present, plus whether extra fields are allowed.

#### Scenario: Schema description
- **WHEN** a schema with a required `decision` field limited to choices `approve` and `reject` is described
- **THEN** the description is JSON-serializable and reports `decision` as required with types `["str"]` and those
  choices

### Requirement: Resume returns validated, merged context
The system SHALL resume an ACTIVE, unexpired checkpoint with validated input, returning a context holding the original
state, the cleaned input, the merge of state overlaid by input (input wins on key collisions), the actor, the resume
time and the state fingerprint. The checkpoint MUST become RESUMED with its version incremented.

#### Scenario: Successful resume
- **WHEN** a checkpoint with state `{"amount": 10, "note": "agent"}` is resumed by `alice` with input
  `{"note": "human", "approved": true}`
- **THEN** the context's merged value is `{"amount": 10, "note": "human", "approved": true}`, its state is the
  original state and its resumer is `alice`
- **AND** the checkpoint is RESUMED with version 2, resumed by `alice` at the clock's time

#### Scenario: Resume without a schema
- **WHEN** a checkpoint without an input schema is resumed with no input
- **THEN** the cleaned input is an empty mapping and the merged value equals the state

#### Scenario: Non-JSON input without a schema is rejected
- **WHEN** a checkpoint without an input schema is resumed with input containing a non-JSON value
- **THEN** the system raises a schema validation error and the checkpoint stays ACTIVE

#### Scenario: Resume requires an actor
- **WHEN** a resume is requested with an empty actor
- **THEN** the system raises a validation error and the checkpoint stays ACTIVE

### Requirement: Events never expose raw human input
The system SHALL publish `checkpoint.resumed` with the run id, step, the fingerprint of the cleaned input and its
keys, and SHALL publish `checkpoint.resume_rejected` with a reason (validation, integrity, state or expired) and an
error list; neither event MUST contain raw human input values. A failing event sink MUST NOT fail the operation.

#### Scenario: Resumed event carries fingerprint and keys only
- **WHEN** a checkpoint is resumed with input `{"secret": "s3cr3t-value"}`
- **THEN** the `checkpoint.resumed` event contains the input fingerprint and keys `["secret"]` and no event data
  contains `s3cr3t-value`

#### Scenario: Rejected event for invalid input
- **WHEN** a resume fails input validation
- **THEN** a `checkpoint.resume_rejected` event with reason `validation` and the error list is published, and it does
  not contain the submitted values

#### Scenario: Failing sink is isolated
- **WHEN** the event sink raises on every event
- **THEN** pause and resume still succeed

### Requirement: Invalid input leaves the checkpoint resumable
The system SHALL keep a checkpoint ACTIVE and unchanged when resume fails input validation, so the human can correct
the input and retry.

#### Scenario: Retry after invalid input
- **WHEN** a resume fails validation and is then retried with valid input
- **THEN** the first attempt raises a schema validation error, the checkpoint remains ACTIVE at version 1, and the
  retry succeeds

### Requirement: Resume is single-use
The system SHALL reject resuming a checkpoint that is not ACTIVE with a state error, publishing a rejected event with
reason `state`, so a decision cannot be replayed.

#### Scenario: Second resume is rejected
- **WHEN** a checkpoint that was already resumed is resumed again
- **THEN** the system raises a state error and the original resume record is unchanged

#### Scenario: Resume after discard is rejected
- **WHEN** a discarded checkpoint is resumed
- **THEN** the system raises a state error

### Requirement: Concurrent resumes are atomic
The system SHALL serialize resumes of the same checkpoint so that, among concurrent attempts, exactly one succeeds and
all others fail with a state error.

#### Scenario: Concurrent resume race
- **WHEN** ten resumes of the same checkpoint are started concurrently
- **THEN** exactly one returns a context and nine raise a state error

### Requirement: Expired checkpoints cannot be resumed
The system SHALL treat a checkpoint as expired when the clock's time is at or after its expiry; resuming it MUST
raise an expired error, mark it EXPIRED, and publish `checkpoint.expired` and a rejected event with reason `expired`.

#### Scenario: Resume after expiry
- **WHEN** a checkpoint with a time-to-live of 10 is resumed 10 seconds after it was paused
- **THEN** the system raises an expired error and the checkpoint status is EXPIRED

#### Scenario: Resume before expiry
- **WHEN** the same checkpoint is resumed 9 seconds after it was paused
- **THEN** the resume succeeds

#### Scenario: Resume of an already expired checkpoint
- **WHEN** a checkpoint already marked EXPIRED is resumed
- **THEN** the system raises an expired error

### Requirement: Resume verifies state integrity
The system SHALL recompute the stored state's fingerprint on resume and raise an integrity error, publishing a
rejected event with reason `integrity` and leaving the checkpoint unchanged, when it differs from the stored
fingerprint or from a fingerprint the caller expects.

#### Scenario: Tampered state is detected
- **WHEN** the stored state is modified directly in the store after pause and the checkpoint is resumed
- **THEN** the system raises an integrity error and the checkpoint is not resumed

#### Scenario: Expected fingerprint mismatch
- **WHEN** a resume supplies an expected fingerprint different from the checkpoint's
- **THEN** the system raises an integrity error

#### Scenario: Expected fingerprint match
- **WHEN** a resume supplies the checkpoint's own state fingerprint
- **THEN** the resume succeeds

### Requirement: Unknown checkpoints are reported as not found
The system SHALL raise a not-found error when getting, resuming or discarding a checkpoint id that does not exist.

#### Scenario: Unknown id
- **WHEN** an unknown checkpoint id is fetched, resumed or discarded
- **THEN** the system raises a not-found error

### Requirement: Checkpoints can be discarded
The system SHALL let an actor discard an ACTIVE checkpoint with an optional reason, marking it DISCARDED, incrementing
its version and publishing `checkpoint.discarded`; discarding a checkpoint that is not ACTIVE MUST raise a state error.

#### Scenario: Discard an active checkpoint
- **WHEN** `bob` discards an active checkpoint with reason `obsolete`
- **THEN** the checkpoint is DISCARDED with discarder `bob` and reason `obsolete`, and a `checkpoint.discarded` event
  with that reason is published

#### Scenario: Discard a resumed checkpoint
- **WHEN** a resumed checkpoint is discarded
- **THEN** the system raises a state error

### Requirement: Overdue checkpoints can be expired in bulk
The system SHALL, on request, mark every ACTIVE checkpoint whose expiry has passed as EXPIRED, publish
`checkpoint.expired` for each, and return them, leaving unexpired and never-expiring checkpoints untouched.

#### Scenario: Expire due checkpoints
- **WHEN** three checkpoints with time-to-lives 5, 50 and none exist and the clock advances by 10
- **THEN** expiring due checkpoints returns only the first, now EXPIRED, and a second call returns nothing

### Requirement: Checkpoints can be listed
The system SHALL list a run's resumable checkpoints (ACTIVE and not past expiry), and SHALL list all ACTIVE
checkpoints in creation order in pages of a bounded size with an opaque continuation cursor that remains stable when
earlier items change status. Invalid page sizes or cursors MUST be rejected.

#### Scenario: Active checkpoints for a run
- **WHEN** a run has one active, one resumed and one overdue checkpoint and another run has an active checkpoint
- **THEN** listing the run's active checkpoints returns only its active, unexpired one

#### Scenario: Paginated listing
- **WHEN** five active checkpoints exist and they are listed with a page size of 2
- **THEN** three pages of 2, 2 and 1 items are returned in creation order and the last page has no cursor

#### Scenario: Cursor stability
- **WHEN** an item on an already-returned page is resumed before the next page is requested
- **THEN** the next page neither skips nor repeats any remaining active checkpoint

#### Scenario: Invalid page parameters
- **WHEN** a listing is requested with a page size below 1 or a malformed cursor
- **THEN** the system raises a validation error

### Requirement: The store enforces optimistic concurrency
The checkpoint store SHALL reject creating a checkpoint whose id already exists and SHALL reject an update whose
expected version differs from the stored version, with a state error, so that multiple managers sharing a store
cannot both transition one checkpoint.

#### Scenario: Stale update rejected
- **WHEN** a checkpoint is updated with an expected version that is not the stored version
- **THEN** the store raises a state error and keeps the stored checkpoint

#### Scenario: Duplicate create rejected
- **WHEN** a checkpoint is created with an id that already exists
- **THEN** the store raises a state error

#### Scenario: Store returns isolated copies
- **WHEN** a checkpoint read from the store is mutated by the caller
- **THEN** a subsequent read returns the unmodified checkpoint

### Requirement: Checkpoints serialize to a mapping
The system SHALL serialize a checkpoint to a JSON-serializable mapping containing its id, run id, step, state, state
fingerprint, schema description, timestamps, status, actors, version and metadata.

#### Scenario: Serialize checkpoint
- **WHEN** a checkpoint with an input schema is serialized
- **THEN** the mapping is JSON-serializable and its status is the string `active`
