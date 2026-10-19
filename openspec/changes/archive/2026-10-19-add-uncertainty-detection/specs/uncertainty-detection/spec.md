## ADDED Requirements

### Requirement: Signal results
A signal result SHALL carry the signal name, a score within [0, 1], a human-readable reason and a flag that
forces review. The system MUST reject a score outside [0, 1] or a non-numeric score with a validation error.

#### Scenario: Valid signal result
- **WHEN** a signal result is created with name "risk", score 0.5 and reason "risk MEDIUM"
- **THEN** it exposes those values and does not force review by default

#### Scenario: Out-of-range score rejected
- **WHEN** a signal result is created with score 1.5 or -0.1
- **THEN** a validation error is raised

### Requirement: Confidence signal
The confidence signal SHALL score an action as `1 - confidence`. Its reason MUST state that confidence is below
the configured threshold (default 0.7) when it is. When an optional floor is configured, an action whose
confidence is below the floor MUST force review.

#### Scenario: Low confidence scored and explained
- **WHEN** an action with confidence 0.4 is evaluated with threshold 0.7
- **THEN** the score is 0.6 and the reason mentions confidence below threshold

#### Scenario: Confidence floor forces review
- **WHEN** an action with confidence 0.3 is evaluated by a confidence signal with floor 0.5
- **THEN** the result forces review

#### Scenario: High confidence passes
- **WHEN** an action with confidence 0.95 is evaluated with floor 0.5
- **THEN** the score is 0.05 and review is not forced

### Requirement: Risk signal
The risk signal SHALL map declared risk to a score (LOW 0.0, MEDIUM 0.33, HIGH 0.67, CRITICAL 1.0) and MUST
force review when the declared risk is at or above the configured review level (default HIGH).

#### Scenario: Risk mapped to score
- **WHEN** actions with each risk level are evaluated
- **THEN** their scores are 0.0, 0.33, 0.67 and 1.0 respectively

#### Scenario: Risk at review level forces review
- **WHEN** an action with HIGH or CRITICAL risk is evaluated with review level HIGH
- **THEN** the result forces review, while LOW and MEDIUM do not

### Requirement: Policy rule signal
A rule signal SHALL evaluate a caller-supplied predicate over the action and context. When the predicate holds
it MUST report the configured score (default 1.0) and reason and force review unless configured not to; when it
does not hold it MUST score 0 without forcing review.

#### Scenario: Matching rule forces review
- **WHEN** a rule "amount over 10000" is evaluated for an action whose amount parameter is 25000
- **THEN** the result has score 1.0, the rule's reason and forces review

#### Scenario: Non-matching rule passes
- **WHEN** the same rule is evaluated for an amount of 50
- **THEN** the result has score 0 and does not force review

#### Scenario: Soft rule contributes score only
- **WHEN** a rule configured with score 0.4 and no forcing matches
- **THEN** the result has score 0.4 and does not force review

### Requirement: Novelty signal
The novelty signal SHALL score 1.0 with a reason starting "novel action" for an action whose shape (name and
sorted parameter keys) has been observed fewer than the configured minimum number of times, and 0 otherwise.
Parameter values MUST NOT affect the shape. Its memory MUST be bounded by a maximum number of tracked shapes,
evicting the least recently used shape when full.

#### Scenario: Unseen action is novel
- **WHEN** an action that was never observed is evaluated
- **THEN** the score is 1.0 and the reason starts with "novel action"

#### Scenario: Observed shape is familiar
- **WHEN** an action is observed and an action with the same name and parameter keys but different values is evaluated
- **THEN** the score is 0

#### Scenario: Minimum observations respected
- **WHEN** the minimum is 2 and the shape has been observed once
- **THEN** the action is still novel, and after a second observation it is not

#### Scenario: Memory is bounded with LRU eviction
- **WHEN** more shapes than the maximum are observed
- **THEN** the number of tracked shapes never exceeds the maximum and the least recently used shape becomes novel again

### Requirement: Disagreement signal
The disagreement signal SHALL read alternative candidate outputs from a context key (default "candidates") and
score `1 - (share of the most common candidate)`, comparing candidates by canonical fingerprint. It MUST score 0
when the key is missing or fewer than two candidates are present, and MUST reject a non-list value.

#### Scenario: Unanimous candidates
- **WHEN** the context holds three structurally equal candidates
- **THEN** the score is 0

#### Scenario: Split candidates
- **WHEN** the context holds candidates "a", "a", "b", "c"
- **THEN** the score is 0.5

#### Scenario: Too few candidates
- **WHEN** the context holds one candidate or no candidates key
- **THEN** the score is 0

#### Scenario: Malformed candidates
- **WHEN** the candidates value is not a list
- **THEN** a validation error is raised

### Requirement: Detector configuration
The detector SHALL accept signals, a threshold in [0, 1] (default 0.5), an aggregation strategy among "max"
(default), "weighted" and "noisy_or", and optional non-negative per-signal weights (default 1.0). It MUST reject
an unknown aggregation, an out-of-range threshold, duplicate signal names and negative weights with a
validation error.

#### Scenario: Invalid configuration rejected
- **WHEN** a detector is created with aggregation "median", threshold 1.2, two signals named "risk", or a weight of -1
- **THEN** a validation error is raised in each case

### Requirement: Score aggregation
The detector SHALL aggregate signal scores using the configured strategy: "max" takes the largest weighted score
(capped at 1), "weighted" takes the weight-normalised mean, and "noisy_or" takes `1 - prod(1 - weighted score)`.
A detector with no signals MUST produce score 0.

#### Scenario: Max aggregation
- **WHEN** signals score 0.2 and 0.6 with max aggregation
- **THEN** the assessment score is 0.6

#### Scenario: Weighted aggregation
- **WHEN** signals score 1.0 (weight 3) and 0.0 (weight 1) with weighted aggregation
- **THEN** the assessment score is 0.75

#### Scenario: Noisy-or aggregation
- **WHEN** signals score 0.5 and 0.5 with noisy_or aggregation
- **THEN** the assessment score is 0.75

#### Scenario: No signals
- **WHEN** a detector without signals assesses an action
- **THEN** the score is 0 and review is not required

### Requirement: Review decision and assessment
Assessing an action SHALL return an immutable assessment with a unique id, the action fingerprint, the action
risk, the aggregated score, the threshold, a creation time from the injected clock, the per-signal results and
the reasons of signals that scored above 0 or forced review, ordered by descending score. Review MUST be
required when the score is at or above the threshold or any signal forces review. The assessment MUST be
serialisable to a plain dictionary.

#### Scenario: Score at threshold requires review
- **WHEN** the aggregated score equals the threshold
- **THEN** the assessment requires review

#### Scenario: Forcing signal overrides a low score
- **WHEN** a weighted aggregate is below the threshold but one signal forces review
- **THEN** the assessment requires review

#### Scenario: Reasons ordered by score
- **WHEN** signals score 0.3, 0.9 and 0
- **THEN** the reasons list the 0.9 reason first, then the 0.3 reason, and omit the zero-scoring signal

#### Scenario: Serialisable assessment
- **WHEN** an assessment is converted to a dictionary
- **THEN** the result is JSON serialisable and contains the id, score, decision, reasons and signal results

### Requirement: Fail-safe signal evaluation
A signal that raises an exception or returns an invalid result SHALL be recorded as score 1.0 forcing review with
reason "signal <name> failed: ..." so that a failure never lets an action pass silently.

#### Scenario: Raising signal forces review
- **WHEN** a signal raises an error during assessment
- **THEN** the assessment requires review and its reasons include "signal <name> failed"

#### Scenario: Invalid return forces review
- **WHEN** a signal returns something that is not a signal result
- **THEN** the assessment requires review

### Requirement: Runtime signal management
The detector SHALL allow adding and removing signals at runtime. Adding a signal whose name is already registered
MUST raise a validation error and removing an unknown signal MUST raise a not-found error.

#### Scenario: Add and remove signals
- **WHEN** a signal is added and later removed by name
- **THEN** subsequent assessments include and then exclude its result

#### Scenario: Invalid management operations
- **WHEN** a duplicate name is added or an unknown name is removed
- **THEN** a validation error or not-found error is raised respectively

### Requirement: Batch assessment
The detector SHALL assess a batch of actions, each optionally paired with a context, asynchronously and return
the assessments in input order.

#### Scenario: Batch preserves order
- **WHEN** a batch of three actions (one with a context) is assessed
- **THEN** three assessments are returned in the same order as the inputs

### Requirement: Assessment events
Every assessment SHALL publish an "uncertainty.assessed" event whose subject is the assessment id and whose data
contains the action name, score, review decision and reasons, timestamped by the injected clock. When review is
required, an additional "uncertainty.review_required" event MUST be published. A failing event sink MUST NOT
prevent the assessment from being returned.

#### Scenario: Events for a reviewed action
- **WHEN** an action requiring review is assessed
- **THEN** "uncertainty.assessed" and "uncertainty.review_required" events are published with the assessment id and clock time

#### Scenario: Events for a passing action
- **WHEN** an action not requiring review is assessed
- **THEN** only "uncertainty.assessed" is published

#### Scenario: Failing sink isolated
- **WHEN** the event sink raises
- **THEN** the assessment is still returned

### Requirement: Default detector
The system SHALL provide a default detector combining a confidence signal with floor 0.5, a risk signal
reviewing at HIGH and a novelty signal, accepting detector options as overrides.

#### Scenario: Default detector behaviour
- **WHEN** the default detector assesses a novel action, then an observed low-risk high-confidence action, then an observed CRITICAL action
- **THEN** the first and third require review and the second does not
