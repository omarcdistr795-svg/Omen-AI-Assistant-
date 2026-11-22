"""Terminal rendering for status, health, tool progress and approval cards."""

from __future__ import annotations

import json
from typing import Any

from ..safety.redaction import redact_value


def render_json(title: str, value: Any) -> str:
    return f"{title}\n{json.dumps(redact_value(value), indent=2, sort_keys=True, default=str)}"


def render_approval_card(card: dict) -> str:
    lines = [
        "APPROVAL CARD",
        f"  token:    {card.get('token_id')}",
        f"  tool:     {card.get('tool')}",
        f"  target:   {card.get('target')}",
        f"  payload:  {json.dumps(redact_value(card.get('payload')), sort_keys=True, default=str)}",
        f"  affected: {json.dumps(redact_value(card.get('affected')), default=str)}",
        f"  risk:     {card.get('risk')}",
        f"  expires:  {card.get('expires_at')}",
        f"  options:  {', '.join(card.get('options') or [])}",
        f"  plan:     {card.get('plan')}",
        "  approve with: /approve <token> | /deny <token> | /dry-run <tool> <json>",
    ]
    return "\n".join(lines)


def render_tool_progress(result: dict) -> str:
    lines = [
        f"TOOL {result.get('tool')} — {result.get('status')}",
        f"  reason:   {result.get('reason')}",
        f"  decision: {result.get('decision')}",
        f"  retries:  {result.get('retries')}",
        f"  duration: {result.get('duration_ms')} ms",
    ]
    if result.get("error"):
        lines.append(f"  error:    {json.dumps(redact_value(result['error']), sort_keys=True, default=str)}")
    if result.get("output") is not None:
        lines.append(f"  output:   {json.dumps(redact_value(result['output']), sort_keys=True, default=str)}")
    if result.get("approval_card"):
        lines.append(render_approval_card(result["approval_card"]))
    return "\n".join(lines)


def render_status(status: dict) -> str:
    resources = status["resources"]
    cost = status["cost"]
    return "\n".join(
        [
            "STATUS",
            f"  assistant: {status['system']['name']} ({status['system']['agent']})",
            f"  RAM:       {resources['ram_used_gb']} / {resources['ram_total_gb']} GB ({resources['ram_percent']}%)",
            f"  CPU:       {resources['cpu_percent']}%  pressure={resources['level']}",
            f"  models:    {', '.join(status['models']['loaded_models']) or 'none'} "
            f"({status['models']['estimated_gb']} GB)",
            f"  degrade:   {status['degradation']['level']}",
            f"  spend:     ${cost['spent_usd']} / ${cost['monthly_budget_usd']} "
            f"({cost['percent_used']}%), cap {cost['token_cap']} tokens/request",
            f"  kill:      {status['safety']['kill_switch']['active']}",
            f"  tasks:     {len(status.get('active_tasks') or [])} active",
            f"  alerts:    {len(status.get('alerts') or [])} open",
            f"  approvals: {len(status.get('pending_approvals') or [])} pending",
            f"  health:    {status.get('system_health', 'unknown')}",
            f"  projects:  {', '.join((status.get('projects') or {}).get('active', [])) or 'none'}",
            f"  memory:    {(status.get('memory_state') or {}).get('implementation', 'unknown')}",
            f"  automation:{(status.get('automation') or {}).get('enabled', False)} "
            f"(jobs={((status.get('automation') or {}).get('scheduler') or {}).get('jobs', 0)}, "
            f"deferred={((status.get('automation') or {}).get('notifier') or {}).get('deferred', 0)})",
            f"  dashboard: {((status.get('dashboard') or {}).get('running', False)) and 'running' or 'stopped'} "
            f"{((status.get('dashboard') or {}).get('url') or '')}",
        ]
    )
