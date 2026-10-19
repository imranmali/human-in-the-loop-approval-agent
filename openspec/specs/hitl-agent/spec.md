# hitl-agent Specification

## Purpose
Provide the guarded execution loop that composes uncertainty detection, approval collection, checkpointed pause/resume and auditing, so that no uncertain action runs without a recorded human decision and a resumed run uses exactly the context the human validated.

## Requirements

### Requirement: Guarded execution
The agent SHALL assess every submitted action for uncertainty before execution and SHALL execute it immediately only when the assessment does not require review.

#### Scenario: Confident low-risk action runs immediately
- **WHEN** an action whose assessment does not require review is submitted
- **THEN** its registered handler is executed and the outcome status is `COMPLETED` with the handler result

#### Scenario: Uncertain action pauses
- **WHEN** an action whose assessment requires review is submitted
- **THEN** the handler is not executed, a checkpoint and an approval request are created, and the outcome status is `PENDING_APPROVAL` with both ids

#### Scenario: Unregistered action
- **WHEN** an action with no registered handler is submitted
- **THEN** a `NotFoundError` is raised before any assessment is recorded

### Requirement: Resume with validated context
The agent SHALL resume a paused run only after the approval request reaches a terminal status, and SHALL execute the action only when the request is approved, applying human modifications that pass validation.

#### Scenario: Approved run executes
- **WHEN** a pending request is approved and the run is resumed
- **THEN** the handler executes with the original action and the outcome status is `COMPLETED`

#### Scenario: Modifications are applied
- **WHEN** an approver approves with modifications that satisfy the modification schema
- **THEN** the handler executes with the modified params and the outcome records the modifications

#### Scenario: Invalid modifications are rejected
- **WHEN** approved modifications fail the modification schema (unknown key or wrong type)
- **THEN** the handler is not executed, a validation error is raised, and the checkpoint remains resumable

#### Scenario: Rejected run never executes
- **WHEN** the request is rejected, expired or cancelled and the run is resumed
- **THEN** the handler is not executed, the checkpoint is discarded and the outcome status is `REJECTED`, `EXPIRED` or `CANCELLED`

#### Scenario: Resume while pending
- **WHEN** a run is resumed while its request is still pending
- **THEN** a `StateError` is raised and nothing executes

#### Scenario: Run resumes only once
- **WHEN** a completed run is resumed again
- **THEN** a `StateError` is raised and the handler is not executed a second time

### Requirement: Approval bound to the exact action
The agent MUST refuse to execute when the action stored in the checkpoint does not match the action fingerprint on the approval request.

#### Scenario: Tampered checkpoint
- **WHEN** the checkpointed action differs from the approved action
- **THEN** an `IntegrityError` is raised and the handler is not executed

### Requirement: Idempotent submission
The agent SHALL return the existing pending request when the same action is submitted again within the same run.

#### Scenario: Duplicate submit
- **WHEN** an identical action is submitted twice with the same run id while pending
- **THEN** both outcomes reference the same approval request id

### Requirement: Blocking run
The agent SHALL provide a blocking `run` that submits, waits for a decision up to an optional timeout, and resumes.

#### Scenario: Run waits for decision
- **WHEN** `run` is awaited and an approver approves concurrently
- **THEN** `run` returns a `COMPLETED` outcome

#### Scenario: Run times out
- **WHEN** no decision arrives within the timeout
- **THEN** `run` returns a `PENDING_APPROVAL` outcome that can be resumed later

### Requirement: Decided-run sweep
The agent SHALL provide `process_decided` to resume every run whose request has reached a terminal status and whose checkpoint is still active, so that stateless workers can drain decisions.

#### Scenario: Sweep resumes decided runs
- **WHEN** two runs are approved and one is still pending and `process_decided` is called
- **THEN** exactly the two decided runs are resumed and their outcomes returned

### Requirement: Handler failures are contained
The agent SHALL report a handler exception as a `FAILED` outcome carrying the error message, without raising.

#### Scenario: Handler raises
- **WHEN** a handler raises an exception during execution
- **THEN** the outcome status is `FAILED` and an `agent.failed` event is audited

### Requirement: End-to-end audit trail
The agent SHALL route every component's events, plus its own lifecycle events, into a single audit log, so that a run's history can be queried by run id and the hash chain verifies.

#### Scenario: Full history for a run
- **WHEN** an action is submitted, paused, approved and resumed
- **THEN** querying the audit log by the run id returns the assessment, pause, approval and execution events in order, and verification succeeds
