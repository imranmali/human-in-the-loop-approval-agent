"""End-to-end demo: an agent issuing refunds pauses for a human on risky ones.

Run: python examples/refund_approval.py
"""

import asyncio
import tempfile
from pathlib import Path

from hitl import (
    Action,
    ApprovalPolicy,
    Approver,
    AuditLog,
    ConfidenceSignal,
    HITLAgent,
    InMemoryApproverDirectory,
    JsonlAuditBackend,
    RiskLevel,
    RiskSignal,
    RuleSignal,
    UncertaintyDetector,
)


async def refund(action, context):
    return f"refunded {action.params['amount']} {action.params['currency']} to {action.params['customer']}"


async def main() -> None:
    audit_path = Path(tempfile.mkdtemp()) / "audit.jsonl"
    detector = UncertaintyDetector(
        [
            ConfidenceSignal(floor=0.6),
            RiskSignal(RiskLevel.HIGH),
            RuleSignal(
                "large-amount",
                lambda action, ctx: action.params.get("amount", 0) > 1_000,
                "refund above 1,000 requires review",
            ),
        ],
        threshold=0.8,
    )
    agent = HITLAgent(
        detector=detector,
        audit=AuditLog(JsonlAuditBackend(audit_path)),
        directory=InMemoryApproverDirectory([Approver("alice", {"finance"}), Approver("bob", {"finance"})]),
        default_policy=ApprovalPolicy(required_roles={"finance"}, timeout_seconds=3600),
    )
    agent.register("payments.refund", refund)

    small = Action("payments.refund", {"amount": 40, "currency": "EUR", "customer": "c-1"}, confidence=0.97)
    print("small refund ->", (await agent.submit(small)).result)

    large = Action("payments.refund", {"amount": 4_800, "currency": "EUR", "customer": "c-2"}, confidence=0.9)
    paused = await agent.submit(large, context={"ticket": "SUP-1234"})
    print("large refund ->", paused.status.value, "| reasons:", list(paused.assessment.reasons))

    # A human reviews the request (e.g. from a dashboard) and lowers the amount.
    await agent.approvals.decide(
        paused.request_id, "alice", "modify", comment="partial refund only", modifications={"amount": 2_400}
    )
    resumed = await agent.resume(paused.request_id, actor="alice")
    print("after approval ->", resumed.status.value, "|", resumed.result)

    print("\naudit trail for", paused.run_id)
    for record in agent.history(paused.run_id):
        print(f"  #{record.seq:<3} {record.kind:<28} actor={record.actor}")
    report = agent.audit.verify()
    print("chain verified:", report.ok, "| head:", agent.audit.head_hash[:16], "| file:", audit_path)


if __name__ == "__main__":
    asyncio.run(main())
