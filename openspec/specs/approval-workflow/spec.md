# approval-workflow Specification

## Purpose
Lets the agent ask humans to approve, reject or modify a proposed action, enforces who may decide and how many
decisions are needed, and resolves requests that are cancelled or not answered in time.

## Requirements

### Requirement: Approval policy validation
An approval policy SHALL require at least one approval, a positive timeout or none, and SHALL reject escalation roles
without a timeout. Invalid policies MUST raise a validation error when constructed.

#### Scenario: Quorum below one is rejected
- **WHEN** a policy is constructed with required approvals of 0
- **THEN** a validation error is raised

#### Scenario: Non-positive timeout is rejected
- **WHEN** a policy is constructed with a timeout of 0 seconds
- **THEN** a validation error is raised

#### Scenario: Escalation without timeout is rejected
- **WHEN** a policy declares escalation roles but no timeout
- **THEN** a validation error is raised

### Requirement: Creating approval requests
The system SHALL create a pending approval request for a proposed action with a unique id, the requester, a reason,
a copy of the review context, a context fingerprint, the policy, the creation time and a deadline of creation time
plus timeout (none when the policy has no timeout).

#### Scenario: Request is pending with a deadline
- **WHEN** a request is created at time 100 with a policy timeout of 60 seconds
- **THEN** the request is pending, has an id prefixed `apr_`, was created at 100 and expires at 160

#### Scenario: Request without timeout never expires
- **WHEN** a request is created with a policy without timeout
- **THEN** its deadline is empty

#### Scenario: Context is copied at creation
- **WHEN** the caller mutates the context mapping after creating the request
- **THEN** the stored request context is unchanged

#### Scenario: Invalid request input is rejected
- **WHEN** a request is created with an empty requester or a non-action
- **THEN** a validation error is raised

### Requirement: Idempotent request creation
Requests created with the same idempotency key SHALL resolve to a single request. Reusing a key with a different
action MUST be rejected.

#### Scenario: Same key returns the existing request
- **WHEN** a request is created twice with the same idempotency key and the same action
- **THEN** both calls return the same request id and only one request is stored

#### Scenario: Same key with a different action is rejected
- **WHEN** a request is created with an idempotency key already used for a different action
- **THEN** a validation error is raised

#### Scenario: Concurrent creation with the same key
- **WHEN** several requests with the same idempotency key are created concurrently
- **THEN** all of them return the same request

### Requirement: Approver authorization
Only known approvers holding at least one eligible role (any role when none is required) SHALL decide. The requester
MUST NOT decide their own request unless the policy allows self-approval.

#### Scenario: Unknown approver is refused
- **WHEN** an approver id not present in the approver directory decides
- **THEN** an authorization error is raised and no decision is recorded

#### Scenario: Approver without an eligible role is refused
- **WHEN** an approver lacking every required role decides
- **THEN** an authorization error is raised

#### Scenario: Self-approval is refused by default
- **WHEN** the requester decides their own request under a policy that disallows self-approval
- **THEN** an authorization error is raised

#### Scenario: Self-approval allowed by policy
- **WHEN** the requester approves their own request under a policy that allows self-approval
- **THEN** the decision is recorded

### Requirement: Recording decisions
The system SHALL record each decision with approver, verdict, comment, modifications and decision time. Decisions on
a non-pending request and repeat decisions by the same approver MUST be refused with a state error.

#### Scenario: Decision is recorded
- **WHEN** an eligible approver approves with a comment
- **THEN** the request holds a decision with that approver, verdict, comment and the current time

#### Scenario: Same approver cannot decide twice
- **WHEN** an approver who already approved decides again on a still pending request
- **THEN** a state error is raised

#### Scenario: Decision on a resolved request is refused
- **WHEN** an approver decides on a request that is already approved, rejected or cancelled
- **THEN** a state error is raised

#### Scenario: Unknown request
- **WHEN** a decision references a request id that does not exist
- **THEN** a not-found error is raised

### Requirement: Decision input validation
A modify verdict SHALL require non-empty modifications, other verdicts MUST NOT carry modifications, and a rejection
MUST carry a comment when the policy requires one. Unknown verdicts MUST raise a validation error.

#### Scenario: Modify without modifications is rejected
- **WHEN** an approver submits a modify verdict with no modifications
- **THEN** a validation error is raised

#### Scenario: Approve with modifications is rejected
- **WHEN** an approver submits an approve verdict with modifications
- **THEN** a validation error is raised

#### Scenario: Reject without comment is rejected
- **WHEN** an approver rejects without a comment under a policy requiring rejection comments
- **THEN** a validation error is raised

#### Scenario: Reject without comment allowed by policy
- **WHEN** an approver rejects without a comment under a policy not requiring rejection comments
- **THEN** the request is rejected

#### Scenario: Unknown verdict
- **WHEN** a decision is submitted with an unrecognized verdict value
- **THEN** a validation error is raised

### Requirement: Rejection veto
A single rejection SHALL immediately resolve the request as rejected, regardless of approvals already recorded.

#### Scenario: Rejection after a partial approval
- **WHEN** a request needs two approvals, one approver approves and another rejects
- **THEN** the request is rejected and has a resolution time

### Requirement: Quorum approval
Approve and modify decisions SHALL count toward the required approvals, and the request SHALL become approved when
the count reaches the quorum.

#### Scenario: Request stays pending below quorum
- **WHEN** a request needs two approvals and one approver approves
- **THEN** the request is still pending with one approval

#### Scenario: Request approved at quorum
- **WHEN** a request needs two approvals and two distinct approvers approve
- **THEN** the request is approved and has a resolution time

### Requirement: Approval with modifications
Modifications from modify decisions SHALL be merged in decision order, later values overriding earlier ones, and the
effective action SHALL be the original action with the merged modifications applied to its parameters.

#### Scenario: Merged modifications
- **WHEN** one approver modifies `{"amount": 50}` and a second modifies `{"amount": 40, "note": "x"}`
- **THEN** the effective modifications are `{"amount": 40, "note": "x"}` and the effective action uses them

### Requirement: Expiry on timeout
A pending request whose deadline has passed and that cannot escalate SHALL become expired. A decision on it MUST
raise an expired error and the request MUST be stored as expired.

#### Scenario: Late decision expires the request
- **WHEN** an approver decides after the deadline of a request without escalation roles
- **THEN** an expired error is raised and the request status is expired

### Requirement: Escalation on timeout
When the policy defines escalation roles, the first timeout SHALL escalate instead of expiring: eligible roles become
the escalation roles, the escalation level becomes 1 and the deadline becomes escalation time plus timeout. A
further timeout MUST expire the request.

#### Scenario: First timeout escalates
- **WHEN** a request with escalation roles passes its deadline and is swept
- **THEN** it stays pending at escalation level 1 with a deadline one timeout after the sweep

#### Scenario: Only escalation roles may decide after escalation
- **WHEN** an approver holding only the original role decides on an escalated request
- **THEN** an authorization error is raised, while an approver holding an escalation role can approve it

#### Scenario: Second timeout expires
- **WHEN** an escalated request passes its new deadline and is swept
- **THEN** it becomes expired

#### Scenario: Late decision triggers escalation
- **WHEN** an approver holding an escalation role decides after the original deadline of an escalatable request
- **THEN** the request escalates first and the decision is recorded

### Requirement: Expiry sweep
The system SHALL provide a sweep that escalates or expires every pending request past its deadline, in batches, and
returns the affected requests. Requests not yet due MUST be left untouched.

#### Scenario: Sweep handles only due requests
- **WHEN** the sweep runs with one overdue request, one request not yet due and one without timeout
- **THEN** only the overdue request is returned and expired

#### Scenario: Sweep spans several batches
- **WHEN** more overdue requests exist than one batch holds
- **THEN** all of them are expired by a single sweep

### Requirement: Cancellation
The requester or an eligible approver SHALL be able to cancel a pending request, recording who cancelled and why.
Other actors MUST be refused and non-pending requests MUST NOT be cancelled.

#### Scenario: Requester cancels
- **WHEN** the requester cancels a pending request with a reason
- **THEN** the request is cancelled with the actor, reason and resolution time recorded

#### Scenario: Unrelated actor cannot cancel
- **WHEN** an actor who is neither the requester nor an eligible approver cancels
- **THEN** an authorization error is raised

#### Scenario: Resolved request cannot be cancelled
- **WHEN** a cancelled or approved request is cancelled again
- **THEN** a state error is raised

### Requirement: Waiting for resolution
Callers SHALL be able to await a request until it reaches a terminal status. The wait MUST return immediately for a
terminal request and MUST raise a timeout error when the timeout elapses first.

#### Scenario: Wait resolves on decision
- **WHEN** a caller waits on a pending request and an approver then approves it
- **THEN** the wait returns the approved request

#### Scenario: Wait on a terminal request
- **WHEN** a caller waits on an already rejected request
- **THEN** the rejected request is returned immediately

#### Scenario: Wait times out
- **WHEN** a caller waits with a short timeout and nobody decides
- **THEN** a timeout error is raised

#### Scenario: Wait resolves on cancellation and expiry
- **WHEN** a caller waits on a request that is then cancelled or expired by a sweep
- **THEN** the wait returns the request in that terminal status

### Requirement: Paginated listing
Stored requests SHALL be listable by status in creation order with a page size limit and an opaque cursor. The
pending work of an approver SHALL list only pending, not overdue requests the approver may still decide.

#### Scenario: Paging through pending requests
- **WHEN** five pending requests are listed with a limit of two
- **THEN** three pages of two, two and one requests are returned and the last page has no cursor

#### Scenario: Listing filters by status
- **WHEN** requests are listed with status approved
- **THEN** only approved requests are returned

#### Scenario: Pending work of an approver
- **WHEN** an approver lists their pending work
- **THEN** requests requiring roles they lack, their own requests and requests they already decided are excluded

#### Scenario: Invalid paging input
- **WHEN** a listing uses a limit below one or a malformed cursor
- **THEN** a validation error is raised

### Requirement: Concurrent decisions are serialized
Concurrent decisions on the same request SHALL be applied one at a time so quorum, veto and duplicate checks always
see the latest state.

#### Scenario: Concurrent approvals beyond quorum
- **WHEN** three approvers approve concurrently a request needing two approvals
- **THEN** exactly two decisions are recorded, the request is approved and the third call raises a state error

#### Scenario: Concurrent duplicate decision
- **WHEN** the same approver approves the same request twice concurrently
- **THEN** exactly one decision is recorded

### Requirement: Context integrity
Each request SHALL carry a fingerprint over its action and context, and a decision on a request whose stored action
or context no longer matches the fingerprint MUST raise an integrity error.

#### Scenario: Tampered context is detected
- **WHEN** the stored context of a pending request is altered and an approver then decides
- **THEN** an integrity error is raised and no decision is recorded

### Requirement: Lifecycle events
Every state change SHALL publish an event to the configured sink with kind `approval.requested`, `approval.decided`,
`approval.approved`, `approval.rejected`, `approval.escalated`, `approval.expired` or `approval.cancelled`, the
request id as subject, the acting party, the action name and status, and the clock time.

#### Scenario: Events for an approval
- **WHEN** a request needing one approval is created and approved
- **THEN** `approval.requested`, `approval.decided` and `approval.approved` are published in order with the action
  name, status and clock timestamp, the decided event carrying approver, verdict and comment

#### Scenario: Events for escalation and expiry
- **WHEN** an escalatable request times out twice
- **THEN** `approval.escalated` and then `approval.expired` are published by the system actor

#### Scenario: Failing sink does not break the workflow
- **WHEN** the event sink raises on every event
- **THEN** requests are still created and decided successfully

### Requirement: Request serialization
A request SHALL convert to a plain dictionary and back without loss so persistent stores can serialize it.

#### Scenario: Round trip
- **WHEN** a request with decisions is converted to a dictionary and back
- **THEN** the result equals the original request
