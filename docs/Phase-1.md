# Phase 1 — Foundation

## Step 0: ask me first (no code yet)
Already decided: Python; Intel Core Ultra laptop with NPU and 16 GB RAM; hybrid
pipeline. Ask me, in one short message, and wait for answers:
1. Operating system and version.
2. Dedicated GPU? Headset, or laptop speakers and microphone?
3. Free disk space for models.
4. Cloud model provider and my monthly budget for it.
5. Is cloud speech-to-text / text-to-speech ever acceptable, or local only?
Then restate the confirmed setup in a short table and begin the work below.

## What to build
- Project structure, git setup, README skeleton, .env.example, config.yaml with
  layered config (defaults, .env, git-ignored local override).
- Core system prompt for Omen's personality.
- Provider adapter interface with at least two working adapters (one
  OpenAI-compatible, one local such as Ollama), swappable via config, with
  model fallback.
- Terminal chat with streaming responses and visible tool progress.
- Tool registry with typed schemas, permission classes, timeouts, retry
  policies, and structured errors; two or three harmless demo tools.
- Safety layer: policy engine, approval gate (Plan -> Confirm -> Execute ->
  Verify -> Report), single-use expiring approvals bound to exact tool/target/
  parameters, dry-run, kill switch, secret redaction.
- Append-only audit log with the required fields.
- Terminal commands: /status, /tools, /health, /approve, /deny, /dry-run,
  /kill, /history.
- Resource layer (see machine and low-memory rules): resource monitor, model lifecycle manager (lazy
  load, idle unload, one heavy model at a time), degradation policy, and a
  hardware benchmark script that records baseline RAM and latency.
- /health and /status show RAM, CPU, loaded models, and degradation state.
- Stub interfaces (documented contracts, in-memory/no-op implementations) for:
  memory, scheduler, voice I/O, event bus, notifier.
- Test suite: provider adapters, safety rules, approval binding/expiry,
  redaction, audit log, resource thresholds, degradation order, model unload.
Do NOT build: real memory, real tools, voice, scheduling,
dashboard.
Extra check: a demo showing a blocked action, an approved action, and a
killed action, all visible in the audit log.

Additional scope (added):
- Cloud cost control (see rules above): monthly budget, token caps, 80% warning,
  100% fallback, spend shown in /status, spend logged in the audit trail.
- Tests: budget warning, hard stop and fallback.

## Detailed requirements for this phase
# PERSONALITY AND COMMUNICATION

- Calm, witty, loyal, precise, dry humour, polished, and concise.
- Address me respectfully.
- Keep spoken replies short and natural; provide detailed output in the
  terminal, files, or dashboard when needed.
- Admit uncertainty rather than guessing.
- Ask one focused clarifying question only when ambiguity could cause an
  incorrect or unsafe action.
- Never fabricate facts, sources, commands, file contents, or results.
- When research is involved, distinguish verified facts, estimates, and opinions.
- Use citations or source links for web research.
- Be proactive without becoming noisy: surface only relevant, timely information.

# SAFETY AND PERMISSIONS

Safety is mandatory and must be enforced outside the model, in the execution
layer.

Permission model:
- Read-only: search, read files, fetch pages, inspect repositories,
  and system diagnostics.
- Low-risk writes: create notes, reminders, local files, drafts, and calendar
  holds.
- External writes: send email, post messages, change calendars, comment on
  GitHub, deploy, or modify external services.
- High-risk: shell commands outside sandbox, deleting files, changing system
  settings, financial actions, publishing, or irreversible operations.
- Always blocked: payment execution, credential
  sharing, and unapproved financial transactions.

For every consequential action: Plan -> Confirm -> Execute -> Verify -> Report

Before execution, show:
- Exact target or recipient.
- Exact command, payload, file path, or action.
- Affected files, accounts, repositories, or systems.
- Risk classification.
- A clear approve / reject / edit option.
- A dry-run option: "show me what you would do."

Approval rules:
- Ask for confirmation before destructive commands, anything involving money,
  sending messages, external writes, deployments, deletion, or irreversible
  actions.
- Bind approval to the exact tool, target, and parameters.
- A change to target or parameters requires fresh approval.
- Approvals must be single-use, expire, and be validated by the execution layer.
- Never infer approval from conversation text alone.

Additional safeguards:
- Command allowlists and sandboxing.
- No unrestricted rm, sudo, privileged operations, or network exfiltration by
  default.
- Rate limits and retry limits.
- Idempotent tool operations where possible.
- Rollback or undo plans for Git, files, infrastructure, and calendar changes.
- Global kill switch: hotkey or spoken command that halts speech, cancels
  pending tool calls, and locks external actions.
- Redact secrets, keys, tokens, passwords, and sensitive personal data before
  logging or sending context to a model.
- Keep secrets in .env files or the OS keychain; never in prompts, code,
  transcripts, logs, or memory.

# AUDITING AND OBSERVABILITY

Append-only, structured audit trail for every meaningful action. For each tool
call, record:
- Timestamp in UTC; unique event ID and trace/session ID.
- User identity and agent identity.
- Model/provider and version.
- Tool name, target resource, and arguments with secrets redacted.
- Policy decision: allowed, blocked, escalated, or pending approval.
- Approval record: approver, time, exact approved action, and expiry.
- Execution result, status, duration, retries, and error information.
- Token usage, cost where available, and resource usage.
- A concise result summary; store large outputs separately with references.

Logs must:
- Be tamper-resistant or append-only.
- Restrict access and record access to the logs.
- Redact sensitive data at write time.
- Support searching past actions and reconstructing an incident.
- Alert on unusual tool usage, repeated failures, first use of sensitive tools,
  and abnormal volume.

# INTERFACE

Terminal and voice are the primary interfaces.

Terminal commands: /brief, /status, /remember, /forget, /approve,
/deny, /history, /tools, /health, /plan, /dry-run, /kill.

Also:
- Streaming responses and visible tool progress.
- Approval cards for risky actions.
- Searchable session history with tool traces and citations.
- Cost, token, latency, and usage panel.
- A later HUD-style web dashboard with live activity, approvals, alerts, memory
  browser, system health, and settings.
- Optional secure phone access later, read-only by default, with push
  notifications and approval capability.

# ARCHITECTURE

Omen runs locally on my PC. Phone access is optional and later.

Input: keyboard / microphone / file / webhook
Voice: wake word -> VAD -> STT -> TTS
Brain: provider adapter -> planner -> tool router -> response
Tools: local tools, MCP-compatible connectors, plugins
Memory: vector search + structured store + file store
Safety: policy engine -> approval gate -> audit log
Scheduler: jobs, triggers, alerts, briefings
UI: terminal first, dashboard later

Requirements:
- The AI model/provider must be swappable through configuration. Do not
  hard-code one provider.
- Support OpenAI-compatible APIs, Anthropic, Ollama, LM Studio, and local
  inference through a common adapter interface.
- Keep wake-word, speech-to-text, text-to-speech, and model layers separate and
  replaceable.
- Typed tool schemas, descriptions, permission classes, timeouts, retry
  policies, and structured errors.
- MCP-style connectors so calendar, email, GitHub, files, and future services
  can be added without changing the core.
- Local-first defaults: audio, transcripts, notes, and memory stay on-device
  unless I explicitly enable a cloud feature.
- Layered configuration: config.yaml for defaults, .env for secrets, and a
  git-ignored local override file for personal settings.
- Fail loudly: log real errors rather than silently swallowing them.
- Health checks, backups, update mechanism, and model fallback.

## About Omen
Omen is one complete, all-in-one personal AI assistant for Soham Bhagat,
modelled on Tony Stark's Jarvis, with the calm, low, slightly eerie voice of
Omen from Valorant. One unified assistant: no personas, no modes, no switch
commands. It understands natural language and picks the right tools itself.
Personality: calm, witty, loyal, precise, dry humour, concise, respectful.
It admits uncertainty, never invents facts, sources, commands or results, and
asks one short question only when a wrong guess could be unsafe.

## How to work (read first)
1. Work on THIS phase only. Do not start, partly build, or get ahead on any
   other phase. When you finish, stop and ask the question at the end.
2. Do not collide with other phases. Each area belongs to one phase:
Phase 1 Foundation: core, config, providers, terminal, tool registry, safety,
  audit, resource layer, and stubs for memory/scheduler/voice/notifier.
Phase 2 Memory: memory storage and retrieval.
Phase 3 Core tools: web, files, notes, reminders, calendar, shell, git, system.
Phase 4 Voice: speech in and out.
Phase 5 Briefings and automation: scheduler, briefings, alerts.
Phase 6 Dashboard: local web dashboard.
Phase 7 Hardening and release: tests, security, docs, backups. No new features.
   You may add files and plug into the interfaces earlier phases left, but do
   not rewrite or restructure what an earlier phase owns. If you truly need to
   change an earlier phase's interface, stop and ask me first.
3. Git: work on a branch named phase-1; merge and tag it
   omen-phase-1-complete only when the phase passes its checks.
4. Dependencies: pin versions. List every new dependency in your report. Never
   upgrade one an earlier phase relies on without asking me.
5. Config: add only your own section to config.yaml. Storage changes must be
   additive and reversible.

## Before you start: checks
Confirm and report: (a) the previous phase is tagged and ALL earlier tests pass,
(b) the interfaces you need exist as documented, (c) required tools, packages,
keys and hardware are available, (d) config and .env.example are valid, (e) the
working tree is clean. If any check fails, stop and tell me. Do not quietly
patch earlier work.

## If something goes wrong
- Missing piece from an earlier phase: stop, name it, propose the smallest fix,
  and wait for my approval.
- Something outside your control (API key, microphone, internet, model): use a
  clearly labelled mock, mark it BLOCKED-EXTERNAL, keep working on the rest, and
  tell me exactly what I must provide.
- Errors: never hide or swallow them. Retry a failing step at most 3 times,
  diagnosing differently each time. Then stop and report the exact error, what
  you tried, and your recommended options.
- Never fix a problem by weakening safety, skipping or deleting tests, turning
  off the policy engine, or hard-coding secrets.
- If two requirements conflict, or the work is bigger than planned, stop and
  ask one focused question before continuing.

## The phase is done only when
- Every item under "What to build" works and you have demonstrated it.
- New tests pass AND all earlier phases' tests still pass.
- No unresolved errors; known limits are listed.
- Safety rules for anything you added are enforced in code and tested.
- No secrets in code, logs, prompts, or memory.
- README and .env.example are updated, and rollback steps are written.

## Always true (every phase)
- Every tool declares a permission class and goes through the Phase 1 safety
  layer (policy engine, approval gate, audit log). No shortcuts.
- Blocked always: payment execution, sharing credentials, unapproved
  financial actions.
- Secrets live only in .env or the OS keychain.
- Local-first: audio, transcripts, notes and memory stay on this computer unless
  I turn on a cloud feature.
- Cloud cost cap: monthly budget, per-request token cap, warning at 80%, and at
  100% stop cloud calls and fall back to the local model, telling me why.

## My machine and low-memory rules
- Intel Core Ultra laptop with NPU, 16 GB RAM, no dedicated GPU assumed.
  Language: Python 3.11+. Pipeline: hybrid (cloud reasoning model by default;
  audio, speech, embeddings, memory and safety stay local; a small quantized
  local model is the offline fallback).
- Budget: Omen under 8 GB total (hard ceiling 9 GB); idle under 1.5 GB. Local
  reasoning model 4-5 GB max, speech-to-text 1 GB, text-to-speech 0.5 GB,
  embeddings 0.3 GB. Only one heavy model loaded at a time; load when needed,
  unload when idle. Use the NPU or integrated GPU only if a benchmark shows a
  real gain.
- Watch RAM and CPU. Warn at 80%, degrade at 85%, emergency at 92%. When short
  on memory, give way in this order: dashboard, heavy scheduled jobs, memory
  re-indexing, local reasoning model (use cloud or smaller), text-to-speech
  (lighter voice, then text only), wake word (push-to-talk only), speech-to-text
  (smaller model). Never degrade: safety layer, approval gate, kill switch,
  audit log, terminal chat. Log every change and tell me in plain words.
- Targets (measure and report honestly): first spoken word under 3 s, short
  command transcribed under 1.5 s, barge-in stops speech under 300 ms. If a
  target can't be met, say so and offer options (smaller model, lighter voice,
  opt-in cloud speech with my consent).

## When you finish
1. Run all checks above, including every earlier phase's tests.
2. Give a short report: what works and how to run it, what was built, test
   results, new dependencies, open issues and BLOCKED-EXTERNAL items, anything I
   must provide, and a preview of Phase 2.
3. Stop and ask: "Phase 1 is complete. Start Phase 2? (yes / no / revise)"
