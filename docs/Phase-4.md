# Phase 4 — Voice

## What to build
Build in stages. Each stage has its own gate and a mini-summary, and I
approve before the next stage starts.

Needs: Phases 1–2 complete and passing (Phase 3 optional).
Voice uses the same brain loop, approval gate, kill switch, and audit log as
the terminal; it adds no shortcuts and no new permissions. Audio and
transcripts stay local by default. Voice style: low, calm, measured, slightly
eerie, concise. STT, TTS, VAD, and wake word are separate replaceable layers,
loaded lazily under the memory budget above.

Stage 4a — Basic voice (half-duplex)
- Push-to-talk with configurable global hotkey, VAD, local STT, local TTS,
  spoken replies, and transcript confirmation before ambiguous or risky voice
  commands.
- Spoken kill command and hotkey kill switch.
- Gate: measured latency vs. the targets above, memory within budget.
Stage 4b — Barge-in and echo control
- Interrupt Omen while it speaks; echo prevention using OS or software echo
  cancellation. Recommend a headset as the default; speaker mode is
  best-effort and must not self-trigger.
- Gate: barge-in under 300 ms with headset; echo test passes; honest report of
  speaker-mode limits.
Stage 4c — Wake word and follow-up window
- Optional wake word with configurable phrase and sensitivity, plus a short
  follow-up window. Must be disableable automatically under memory pressure.
- Gate: false-trigger rate measured over an idle test.
Stage 4d — Optional speaker identification (lowest priority)
- Restrict sensitive commands to my voice. Skip or defer this stage if the
  hardware budget is exceeded, and say so.
Tests (all stages): recorded-sample pipeline, barge-in, echo guard,
confirmation, voice kill switch, degradation (lighter voice, text-only,
push-to-talk fallback), regression of earlier phases.
Hardware blocker rule: if no microphone or speaker is available, build against
mock audio devices, mark BLOCKED-EXTERNAL, and tell me what to connect.
Do NOT build: scheduling, briefings, dashboard.

## Detailed requirements for this phase
# VOICE INTERFACE

- Full voice interaction: speech input and spoken responses.
- Push-to-talk by default, using a configurable global hotkey.
- Optional wake word, configurable phrase and sensitivity.
- Barge-in: I must be able to interrupt Omen while it is speaking.
- Prevent Omen from triggering itself through speaker echo.
- Voice activity detection and a short follow-up window after responses.
- Local/offline fallback for speech recognition and speech synthesis.
- Show a transcript of what Omen heard before executing ambiguous or risky
  voice commands.
- Optional speaker identification so sensitive system commands can be
  restricted to my voice.
- Voice style: low, calm, measured, slightly eerie, minimal filler, concise.
- Audio, transcripts, and voice settings remain local by default.

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
3. Git: work on a branch named phase-4; merge and tag it
   omen-phase-4-complete only when the phase passes its checks.
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
   must provide, and a preview of Phase 5.
3. This is the Minimum Viable Omen point: tell me what is usable now.
4. Stop and ask: "Phase 4 is complete. Start Phase 5? (yes / no / revise)"
