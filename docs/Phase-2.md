# Phase 2 — Memory

## What to build
Needs: Phase 1 complete and passing.

- Implement the memory interface: structured store + vector search + file
  store, using lightweight embedded storage (no heavy database server) and a
  small embedding model within the memory budget above.
- Indexing and re-embedding run in the background, are pausable by the
  resource monitor, and never block chat.
- Profile, projects, preferences, past analyses,
  conversation summaries, and entity memory.
- Dated, searchable, editable, attributable items; expiry/de-emphasis of
  transient facts.
- "remember," "update," "forget," "show memory" commands; export and backup;
  memory-review view in the terminal.
- Secret-rejection: memory refuses to store keys, passwords, tokens, or other
  secrets.
- Retrieval wired into the brain loop through the existing interface only.
- Tests: CRUD, search, forget, expiry, export/restore, secret rejection,
  memory use stays within budget on a realistic data size, regression of
  Phase 1.
Do NOT build: tools, voice, scheduling, dashboard.

## Detailed requirements for this phase
# MEMORY

Persistent, searchable, editable memory.

Maintain:
- Personal profile: name, location, timezone, communication preferences, work
  context, and standing preferences.
- Projects: goals, repositories, stakeholders, deadlines, decisions, open
  questions, blockers, and progress.
- Preferences: coding stack, briefing time, notification preferences, research
  style, risk tolerance, and voice verbosity.
- Past analyses: research, code decisions, incident notes, and lessons.
- Conversation memory: concise summaries rather than unbounded raw transcripts,
  linked to source messages, files, and tool results.
- Entity memory: people, companies, tools, repositories, documents, and topics.

Memory rules:
- Every memory item must be dated, searchable, editable, and attributable.
- Support "remember this," "update this," "forget this," and "show memory."
- Expire or de-emphasise transient facts automatically.
- Provide memory export, backup, and a memory-review interface.
- Never store API keys, passwords, tokens, or other secrets
  in memory, prompts, transcripts, or logs.

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
3. Git: work on a branch named phase-2; merge and tag it
   omen-phase-2-complete only when the phase passes its checks.
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
   must provide, and a preview of Phase 3.
3. Stop and ask: "Phase 2 is complete. Start Phase 3? (yes / no / revise)"
