## 1. Agent model

- [x] 1.1 Add `RunStatus` and `Outcome` with `to_dict()`
- [x] 1.2 Add handler registration with an optional `InputSchema` and `ApprovalPolicy` per action, and derive the default modification schema from params

## 2. Execution loop

- [x] 2.1 Implement `submit`: assess, execute directly or pause + request approval (idempotent per run)
- [x] 2.2 Implement `resume`: check terminal status, verify binding fingerprint, validate modifications through the checkpoint, execute or discard
- [x] 2.3 Implement blocking `run` with a timeout, and the `process_decided` sweep
- [x] 2.4 Contain handler failures as `FAILED` outcomes

## 3. Audit wiring

- [x] 3.1 Share one `EventBus` across components, subscribe the `AuditLog`, emit `agent.*` lifecycle events carrying `run_id`

## 4. Verification

- [x] 4.1 Tests for every spec scenario, including an end-to-end audit verification
- [x] 4.2 Export the public API from `hitl/__init__.py` and document it in the README
