"""Demonstrate a blocked, an approved and a killed action in the audit log."""

from __future__ import annotations

import json

from omen.app import OmenApp


def run() -> list[dict]:
    app = OmenApp(output=lambda text: print(text, flush=True))
    app.trace_id = "demo-phase1"
    app.audit.trace_id = "demo-phase1"

    print("\n=== 1. BLOCKED: attempted payment-linked action ===")
    blocked = app.run_tool("demo.note_create", {"title": "payment", "body": "do not execute"})

    print("\n=== 2. APPROVED: exact, single-use approval ===")
    approved_params = {"title": "approval", "body": "approved note"}
    pending = app.run_tool("demo.note_create", approved_params, target="local")
    token = app.approvals.pending.get(pending.token_id)
    if token is None:
        raise RuntimeError("Demo failed: no approval token was issued")
    print(app.approve(token.token_id))
    approved = app.run_tool(
        "demo.note_create", approved_params, target="local", approval_token=token
    )
    print("Replay attempt (must fail):", app.approvals.validate(token, {
        "tool": "demo.note_create", "target": "local", "params": approved_params
    }))

    print("\n=== 3. KILLED: global kill switch halts further actions ===")
    app.kill("demo kill switch")
    killed = app.run_tool("demo.echo", {"text": "should not run"})

    events = app.history(trace_id="demo-phase1")
    print("\n=== AUDIT EVIDENCE ===")
    for event in events:
        if event.get("event_type") in ("tool_call", "approval", "kill_switch"):
            print(
                json.dumps(
                    {
                        "event_id": event["event_id"],
                        "tool": event.get("tool"),
                        "decision": event.get("decision"),
                        "status": event.get("status"),
                        "summary": event.get("summary"),
                    },
                    sort_keys=True,
                )
            )
    statuses = (blocked.status, approved.status, killed.status)
    if statuses != ("blocked", "ok", "blocked"):
        raise RuntimeError(f"Demo did not produce expected statuses: {statuses}")
    return events


def main() -> int:
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
