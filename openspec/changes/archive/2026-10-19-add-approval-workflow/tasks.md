# Tasks

## 1. Data model

- [x] 1.1 Implement `ApprovalStatus`, `Verdict`, `Decision`, `Approver`, `ApprovalPolicy` (with validation) and verify the policy validation tests pass
- [x] 1.2 Implement `ApprovalRequest` with `approvals_count`, `effective_modifications`, `effective_action`, fingerprint verification and `to_dict`/`from_dict`, and verify the round-trip and modification tests pass

## 2. Pluggable backends

- [x] 2.1 Implement `ApproverDirectory` protocol and `InMemoryApproverDirectory` and verify authorization tests resolve approvers through it
- [x] 2.2 Implement `Page`, `ApprovalStore` protocol and `InMemoryApprovalStore` with idempotency and status indexes and sequence cursors, and verify the paging and status filter tests pass

## 3. Approval service

- [x] 3.1 Implement `ApprovalService.request` with idempotency keys and creation events and verify the creation and idempotency tests pass
- [x] 3.2 Implement `ApprovalService.decide` with expiry-first check, integrity check, authorization, veto, quorum and per-request locking, and verify the decision, veto, quorum and concurrency tests pass
- [x] 3.3 Implement timeout handling (expire or escalate once) and `expire_due` batch sweep and verify the expiry, escalation and sweep tests pass
- [x] 3.4 Implement `cancel`, `wait` and `pending_for` and verify the cancellation, waiting and pending-work tests pass
- [x] 3.5 Publish `approval.*` lifecycle events with isolated sink failures and verify the event tests pass

## 4. Verification

- [x] 4.1 Run `python3 -m pytest -q tests/test_approvals.py` and verify every spec scenario has a passing test
- [x] 4.2 Run `ruff check` and `ruff format --check` on the owned files and `openspec validate add-approval-workflow --strict --no-interactive` and verify all pass
