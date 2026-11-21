# Omen — Personal AI Assistant

Omen is a local-first personal AI assistant for Soham Bhagat, modeled on Tony
Stark's JARVIS with the calm, low, slightly eerie presence of Omen.

- **Phase 1 Foundation**: configuration, provider adapters, tool registry, safety
  lifecycle, audit logging, resource monitoring, degradation order, and terminal chat.
- **Phase 2 Memory**: persistent, searchable, editable, attributable memory using
  lightweight embedded SQLite, FTS5 full-text search, dense vector embeddings,
  file store, secret rejection, transient decay, and background indexing.
- **Phase 3 Core tools**: web, files, local notes/reminders/tasks/calendar,
  sandboxed shell, Git, system control, rollback plans and dev helpers. Every
  tool is permission-classed and passes through the Phase 1 safety lifecycle.

Phase 5 adds briefings and automation. Phase 6 adds a local, authenticated,
HUD-style web dashboard that reads and acts only through existing public
interfaces. Phase 7 adds release hardening, verifies backup and recovery paths,
and documents setup, update, and rollback procedures. See
[docs/PHASE7_RUNBOOK.md](docs/PHASE7_RUNBOOK.md).

## Setup

Python 3.11+ is required. The only runtime dependency is pinned in
`pyproject.toml` (`PyYAML==6.0.3`).

```bash
python3 -m pip install -e .          # or install PyYAML==6.0.3 directly
cp .env.example .env                 # optional; secrets only, git-ignored
python3 -m omen.cli.main             # start interactive terminal chat
```

Layered configuration:

1. `config.yaml` — checked-in defaults (no secrets).
2. `config.local.yaml` — git-ignored personal overrides, deep-merged.
3. `.env` — secrets, injected into the adapter named by `api_key_env`.

No cloud provider is enabled by default: monthly budget is $0 and the default
adapter is local Ollama. Cloud calls are stopped by the cost controller and fall
back to the local model.

## Terminal Commands

| Command | Purpose |
|---|---|
| `/status` | Full status: RAM, CPU, models, degradation, spend, tasks, alerts, approvals, health, recent actions, projects, memory, automation, dashboard |
| `/dashboard [start|stop|status]` | Start/stop/status for the local authenticated HUD dashboard |
| `/health` | Configuration, provider, tool, safety, memory, and resource checks |
| `/tools` | Registered tools with permission classes and schemas |
| `/plan <tool> <json>` | Create an approval-bound plan for a tool action |
| `/approve <token>` | Approve one exact pending action (single use) |
| `/deny <token>` | Deny a pending action |
| `/dry-run <tool> <json>` | Show what an action would do without executing |
| `/kill` | Engage the global kill switch |
| `/history [filters]` | Search the append-only audit trail |
| `/brief [morning\|evening]` | Full local briefing / review (defaults to morning) |
| `/guardian` | Check deadlines now: escalation, conflicts, checklists |
| `/alerts` | Scan the audit trail for regressions and long jobs |
| `/remind-repo <message>` | Remind me when I open this repository |
| `/remind-meeting <message>` | Remind me before my next meeting |
| `/remember <text>` | Store a new factual memory item (enforces secret rejection) |
| `/update <id> <text>` | Update an existing memory item |
| `/forget <id>` | Remove a memory item from database, FTS, and vector index |
| `/memory [query]` | Hybrid recall (keyword FTS + vector similarity) |
| `/show-memory` | Alias for the memory review view (`/show memory` also works) |
| `/review-memory` | Terminal memory review view (profile, projects, preferences, entities) |
| `/export-memory [path]` | Export all memory items and entities to JSON |
| `/backup-memory [path]` | Create a compressed `.tar.gz` archive of database and files |
| `/bye` | Exit |

## Memory Architecture (Phase 2)

Persistent, searchable, editable, attributable memory:

- **Structured Store**: Embedded SQLite (`state/memory/omen_memory.db`) with
  ACID compliance, schema versioning, foreign keys, and WAL mode.
- **Full-Text Search (FTS5)**: BM25-ranked full-text keyword indexing across
  memories, kinds, and metadata.
- **Vector Search**: Compact dense vector embeddings (128 dimensions) with
  cosine similarity search. Operates within the 0.3 GB memory budget (< 2 MB RAM).
- **File Store**: Local directory store (`state/memory/files/`) for raw notes,
  attachments, past analysis reports, and compressed backups.
- **Domain Specializations**:
  - *Personal profile*: name, location, timezone, communication style.
  - *Projects*: goals, repositories, stakeholders, deadlines, decisions, blockers.
  - *Preferences*: coding stack, briefing time, notifications, verbosity.
  - *Past analyses*: research findings, code decisions, incident notes, lessons.
  - *Conversation summaries*: concise summaries linked to turns rather than raw transcripts.
  - *Entity memory*: people, companies, tools, repositories, documents, topics.
- **Safety & Secret-Rejection**: Memory strictly refuses to store API keys,
  passwords, tokens, private keys, or credentials. Throws `SecretRejectedError`.
- **Transient Facts & Expiry**: Automatic detection of transient facts with
  exponential half-life decay. Expired items are automatically excluded from recall.
- **Non-blocking Background Indexing**: Vector indexing runs on a background
  queue so chat turns are never blocked. Automatically pauses when the resource
  monitor reports pressure (`degrade` or `emergency`).
- **Brain Loop Retrieval**: Chat queries automatically retrieve relevant context
  and inject it into the model prompt after secret redaction. If recall fails,
  the error is audited and reported in plain words; chat stays available.

## Memory quick demo

```bash
python3 -m omen.cli.main      # start terminal chat (no Ollama required for commands)
/remember Soham prefers Python 3.11 with local SQLite
/memory Python
/review-memory
/export-memory               # JSON written under state/memory/exports/
/backup-memory               # .tar.gz written under state/memory/backups/
```

## Phase 3 core tools

All 56 tools are registered on the Phase 1 registry at startup. They are
partitioned into `read_only`, `low_risk_write`, `external_write` and
`high_risk`; writes always require an exact, single-use approval token. The
full lifecycle (plan -> confirm -> execute -> verify -> report) and append-only
audit log are unchanged.

| Area | Tools |
|---|---|
| Web | `web.search`, `web.read`, `web.summarise`, `web.compare`, `web.save_note` |
| Files | `files.read`, `files.search`, `files.summarise`, `files.list`, `files.draft`, `files.update`, `files.delete` |
| Local data | `notes.*`, `reminders.*`, `tasks.*`, `calendar.*` (create/get/list/search/update/delete; `complete` for reminders/tasks) |
| Shell | `shell.status`, `shell.run` |
| Git | `git.status`, `git.diff`, `git.history`, `git.branches`, `git.commit`, `git.pr_draft` |
| System | `system.status`, `system.open_app`, `system.media`, `system.window` |
| Rollback | `rollback.plan`, `rollback.undo` |
| Dev/AI | `code.inspect`, `repo.context`, `tests.run`, `data.summarise` |

Example (terminal chat):

```bash
python3 -m omen.cli.main
/tools
/plan web.search {"query":"local-first safety"}
/plan files.draft {"filename":"report.md","content":"Draft..."}
/approve <token-id>
/dry-run shell.run {"command":"pwd"}
```

Or from Python:

```python
from omen.app import OmenApp
app = OmenApp()
result = app.run_tool("web.search", {"query": "local-first"})
pending = app.run_tool("notes.create", {"title": "Follow-up", "body": "Ask Soham"})
token = app.approvals.pending[pending.token_id]
app.approve(pending.token_id)
done = app.run_tool("notes.create", {"title": "Follow-up", "body": "Ask Soham"}, approval_token=token)
print(done.output, done.decision)
```

### Web: BLOCKED-EXTERNAL by default

Real web access (search, page read and therefore live citation) requires an
opt-in MCP-style connector in `core_tools.connectors` plus a non-zero enabled
cloud budget. Until then the tools use a clearly labelled offline mock corpus
(`source: mock_offline_corpus`, `blocked_external: true`) with reserved
`.invalid` URLs. Summaries are extractive and carry numbered citations to the
exact pages read; nothing is invented. Provide a connector and a budget in
`config.local.yaml` plus any key in `.env` to unblock live web access.

### Files, shell, Git and system safety

- File paths are confined to `core_tools.files.allowed_roots`, with symlink and
  size checks. Reads are redacted before display or audit.
- `files.update` keeps a timestamped backup; `files.delete` moves to quarantine.
  Nothing is permanently erased by a tool.
- `shell.run` uses argv (no shell interpreter), an allowlist, a mandatory deny
  list (rm/sudo/network tools/interpreters/Git), a scrubbed environment, a
  timeout and output caps. `git`, `rm`, `sudo`, `curl`, `wget`, `nc`, `ssh` and
  interpreter commands are denied. `tests.run` only runs exact pre-registered
  commands from `core_tools.tests.commands`.
- Git tools never push, fetch, pull, reset or clean. Commits stage explicit
  paths only and record a soft-reset undo plan. PR drafts are local files, not
  API calls.
- System control is empty/off by default (`open_apps: {}`, no media or window
  command). Every action is approval-gated and uses argv with no shell.

### Rollback

Write tools return a `rollback_plan_id`. Inspect with `rollback.plan` and undo
with `rollback.undo` (approval required, one shot):

```bash
/plan rollback.plan {"plan_id":"undo_..."}
/plan rollback.undo {"plan_id":"undo_..."}
/approve <token-id>
```

Undo covers files (restore backup / move back from quarantine), local records
(restore previous snapshot, including calendar events) and Git commits
(`git reset --soft` only when HEAD still matches the recorded commit). Shell,
window and media actions are not reversible and are labelled as such.

## Voice (Phase 4)

Voice uses the same `OmenApp.chat()` brain loop, tool policy, approval gate,
kill switch and audit log as terminal chat. Audio and transcripts stay local;
temporary recordings and synthesized WAV replies are removed after use.
Push-to-talk is the default. The Linux adapter uses PipeWire (`pw-record` and
`pw-play`) with a configurable global key in `voice.hotkey`. VAD, Vosk STT and
Piper TTS are separate lazy-loaded adapters. Optional speech dependencies are
pinned in the `voice` extra:

```bash
python3 -m pip install -e '.[voice]'
```

Place a Vosk English small model at `state/models/vosk-model-small-en-us-0.15`
and a compatible Piper voice plus its JSON config at the paths under `voice` in
`config.yaml`. No speech data is sent to cloud services. Start Omen with
`python3 -m omen.cli.main` and hold the configured key to speak. Risky or
low-confidence transcripts are shown for confirmation before the normal brain
loop runs. “Omen, stop” engages the existing kill switch. `/status` reports
model availability and any `BLOCKED-EXTERNAL` setup requirement.

At 85% resource pressure, Omen uses `voice.tts.light_model_path` if that local
model is present, otherwise it switches to text-only replies. At emergency
pressure it always uses text-only. Push-to-talk, speech input and all safety
controls remain available. Each change is reported and audited. The VAD is an
energy-based local detector. Barge-in stops current playback when push-to-talk
is pressed. In speaker mode, the software echo guard suppresses transcripts
matching recent Omen speech; headset use is recommended, and speaker mode is
best-effort. For OS echo cancellation, set `voice.record_target` to an existing
PipeWire echo-cancel source. Optional wake word uses local Vosk grammar
recognition with configurable `voice.wake.phrase` and `sensitivity`. A short
follow-up capture runs after replies; both wake listening and automatic
follow-up stop under resource pressure, leaving push-to-talk active.

### Phase 4 external validation

This checkout reports **BLOCKED-EXTERNAL** for live audio and model validation.
The saved machine context lists a built-in microphone and speakers, but this
runtime cannot access the PipeWire service or audio devices. To validate on the
host, make these available to the Omen process:

- A local microphone and speaker or headset visible to PipeWire (`pw-record`
  and `pw-play`). Use a headset by default. For OS echo cancellation, connect
  or configure a PipeWire echo-cancel source and set its name as
  `voice.record_target`. Speaker mode remains best-effort.
- Read access to the configured global hotkey device (the current default is
  `ALT+V`). If `/dev/input` is restricted, use a permitted hotkey
  provider or grant the Omen user access to the keyboard device.
- Install the pinned `voice` extra and put the Vosk small English model and
  Piper medium voice plus JSON config at the configured paths under
  `state/models/`. To keep TTS available at 85% resource pressure, also provide
  the configured lighter Piper model and JSON config.

Mock checks meet the timing targets (sample STT pipeline: 0.17 ms; simulated
barge-in: below 300 ms). The idle fixture had 0 false triggers across 600
recognizer outputs representing 10 minutes (0/hour). These are mock results;
first spoken word latency, real STT latency, memory with models loaded, acoustic
echo, and real idle false-trigger rate still need host measurement.

Speaker identification (Stage 4d) is **deferred**. No local speaker-verification
model is installed or available to benchmark here. Adding an unmeasured model
could exceed Omen's 8 GB ceiling alongside the configured STT (1 GB), TTS
(0.5 GB), memory (0.3 GB) and reasoning model (4.5 GB). Sensitive tools
therefore continue to use the existing policy and approval checks; Omen does
not claim to verify speaker identity.

To roll back voice, set `voice.enabled: false` in `config.local.yaml` or revert
the Phase 4 commit. Model files in `state/models/` are user data and are not
removed by rollback.

## Phase 5 — briefings and automation

Phase 5 fills the scheduler and notifier stubs with real, local-only services.
It adds no dashboard and makes no cloud calls.

- **Scheduler** (`omen/automation/scheduler.py`): jobs with `at`, `daily`,
  `interval` and `event` triggers, bounded exponential-backoff retries, atomic
  state under `state/automation/jobs.json`, and an audit event on every attempt.
  Heavy jobs defer at `degrade`/`emergency` resource levels; all jobs run at low
  priority while a model is loaded. Job handlers that act on the world are sent
  through the Phase 1 tool registry, so policy -> approval -> audit still apply.
- **Notifier** (`omen/automation/notifier.py`): four priorities
  (`low`/`normal`/`high`/`critical`), `terminal`, `log` and opt-in `voice`
  channels, configurable quiet hours with deferred delivery, deduplication by
  key, a repeat cap and an hourly rate limit. Voice is best-effort: if Phase 4
  voice is absent, disabled or under resource pressure, the notification falls
  back to the terminal and the reason is audited.
- **Briefings** (`omen/automation/briefings.py`): `/brief morning` reports
  calendar, tasks, deadlines, weather (off by default) and project blockers;
  `/brief evening` reports completed work, unresolved decisions, tomorrow's
  priorities and open risks. All data comes from the Phase 3 local store and
  the Phase 1 audit log. With no weather connector, weather is explicitly
  `BLOCKED-EXTERNAL` — never invented.
- **Deadline guardian** (`omen/automation/guardian.py`): escalating reminders
  (24 h / 2 h / 30 m / overdue by default), meeting/deadline conflict detection
  and preparation checklists.
- **Build alerts** (`omen/automation/alerts.py`): CI failures, deployment
  failures, test regressions (audit scan) and long-running jobs, deduplicated
  through the notifier.
- **Contextual reminders** (`omen/automation/contextual.py`): "remind me when I
  open this repository" and "before my next meeting", persisted locally.
- **Status** (`/status`): active tasks, alerts, pending approvals, system
  health, recent actions, projects, memory state and automation state.
- **Tools**: nine additive Phase 5 tools (`brief.morning`, `brief.evening`,
  `status.report`, `guardian.check`, `build_alerts.scan`, `automation.schedule`,
  `automation.cancel`, `notify.send`, `contextual.remind`) declare a permission
  class and are registered on the Phase 1 tool registry with specs.

Configuration lives only in the new `automation:` section of `config.yaml`.
Set `automation.enabled: false` in `config.local.yaml` to restore the original
no-op scheduler/notifier and the legacy short brief.

```bash
# Demonstrate the full stack without any external service
python3 -m unittest tests.test_phase5 -v

# In the terminal: /brief morning, /brief evening, /guardian, /alerts, /status
omen
omen> /brief morning
omen> /status
```

An approval-bound write can be executed from Python through the same safety
layer that the model uses:

```python
from omen.app import OmenApp

app = OmenApp()
params = {
    "name": "demo",
    "when": "2030-01-01T00:00:00+00:00",
    "payload": {"kind": "notification", "message": "hello"},
}
pending = app.run_tool("automation.schedule", params)          # pending_approval
token = app.approvals.approve(pending.token_id, app.user)      # exact, single-use
result = app.run_tool("automation.schedule", params, approval_token=token)
print(result.status, app.brief("morning")["kind"], app.status()["automation"])
```



## Phase 6 — Dashboard

Phase 6 adds a lightweight local HUD-style web dashboard. It is the first thing
to shed under memory pressure and it must not load any model.

- **Local-only by default**: binds to `127.0.0.1` with authentication.
- **Public interfaces only**: reads status/health/history/alerts/memory through
  existing app/public interfaces; approvals go through `app.approve()` /
  `app.deny()` and the same Phase 1 approval gate. No new bypass path.
- **Panels**: live activity feed (SSE), approvals, alerts, memory browser,
  system health, usage/cost, settings, recent actions.
- **No secrets exposed**: API responses pass through redaction; dashboard token
  is read from `.env` (`OMEN_DASHBOARD_TOKEN`) and never logged.
- **Optional secure phone access**: OFF by default. Only when explicitly enabled
  with `dashboard.allow_remote=true`, TLS cert/key configured, and read-only by
  default (`remote_read_only=true`). Push notifications remain opt-in and are not
  enabled by default.

Start:

```bash
# in .env, set OMEN_DASHBOARD_TOKEN to a long random value
python3 -m omen.cli.main
omen> /dashboard start
# open http://127.0.0.1:8765/?token=<token>
```

Rollback: set `dashboard.enabled: false` in `config.local.yaml`, stop the
server, or revert the Phase 6 commit. Dashboard has no persistent data store;
it only reads existing state.
## Tests

```bash
python3 -m unittest discover -s tests -v
```

The test suite uses standard-library `unittest` only and covers:
- Configuration, adapters, cost caps, degradation, safety policies, approval binding, audit logs.
- Memory CRUD, FTS5 keyword search, semantic vector search, kind filtering.
- Domain memories (profile, projects, preferences, analyses, conversation summaries, entities).
- Expiry, transient fact de-emphasis, and purging.
- Secret-rejection on API keys, passwords, tokens, private keys, and metadata.
- Non-blocking background indexing and pausing under resource monitor pressure.
- File store, export/restore roundtrip, and backup archive creation.
- Realistic dataset budget verification (1,000 items stay well under 10 MB).
- Brain loop retrieval integration and terminal commands.
- Entity export/restore roundtrip and non-silent reporting of recall failures.
- Phase 3: all 56 tools' registration, permission classes, dry runs and
  approval requirements; web mock/citations; file rollback; local CRUD for all
  four domains; shell sandbox denial; Git commit undo; system fail-closed
  behaviour; dev helpers; and safety regression (blocked/killed/audited).
- Phase 4: recorded-sample voice pipeline, transcript confirmation, spoken
  kill switch, barge-in, echo guard, configurable local wake phrase and
  follow-up, resource-pressure fallback, and false-trigger fixture measurement.
- Phase 5: trigger scheduling, cancellation and persistence; retry/backoff and
  failure handling; heavy-job deferral and low priority under load; quiet hours,
  priorities, channels, deduplication, spam/rate limits and voice fallback;
  morning/evening briefings; deadline escalation, conflicts and checklists;
  CI/deployment/regression/long-job alerts; repository and meeting reminders;
  `/brief` and full `/status`; and safety regression (blocked/killed/audited).

## Known limits (Phase 2 + Phase 3 + Phase 4)

- The default `local_hash_v1` embedder is a deterministic subword/token hashing
  vectoriser, not a neural language model. Ranking is based on lexical/subword
  overlap. Set `memory.embedding_model: ollama` with a locally running Ollama
  embedding model for denser semantic recall; both options stay on this computer
  and fit the 0.3 GB embedding budget.
- If the Ollama embedding endpoint is unavailable, the adapter logs a warning and
  falls back to `local_hash_v1`. Explicit `wait_for_index=True` calls embed
  synchronously; ordinary chat writes use the background queue.
- The file store checks UTF-8 text for secrets. Opaque binary files cannot be
  inspected and must never be used for credentials.
- Indexing queues are per-process. A second Omen process sharing the same
  database has its own queue; SQLite WAL keeps writes consistent.
- `/forget` is a hard delete. Keep `/backup-memory` snapshots if recovery is needed.
- Memory lives in `state/memory/` on this computer; no cloud memory feature is
  enabled in this phase.
- **Phase 3 web is BLOCKED-EXTERNAL**: no live search/read, no live citation and
  no email connector without an explicitly configured external connector and a
  non-zero cloud budget. Provide these to unblock (see above).
- **Phase 3 parsers**: files/summarisers handle UTF-8 text, code, CSV and JSON.
  PDF, spreadsheet, image extraction and opaque binary inspection are not
  installed. Alarms/timers, briefings and dashboards belong to later phases.
- **Phase 3 system control** fails closed when no app/media/window command is
  configured; window actions are not reversible.
- **Phase 3 local data** lives in `state/phase3/`. Only UTF-8 text is accepted,
  and secrets are rejected before storage using the Phase 2 validator.
- **Phase 4 live audio is BLOCKED-EXTERNAL** in this sandbox. Install the pinned
  speech extra, add the local Vosk/Piper models, and expose PipeWire plus a
  readable hotkey device as listed above. The built-in microphone/speakers are
  documented in the saved machine context; connect a headset for best results.
- **Phase 4 measurements** are mock-only here. The reported idle rate is from a
  synthetic recognizer-output fixture, not an ambient microphone recording.
- **Phase 4 speaker identification** is deferred because no local model could
  be measured against the Omen memory budget; identity is never assumed.
- **Phase 5 weather is BLOCKED-EXTERNAL** until a local connector is supplied;
  the default mock reports unavailability and never invents a forecast.
- **Phase 5 CI/deployment feeds are BLOCKED-EXTERNAL**: `build_alerts.scan`
  reads the local audit trail, and `BuildAlerts.ingest` accepts events from a
  user-supplied local adapter. No network hook or cloud connector is enabled.
- **Phase 5 scheduler** is tick-driven (`scheduler.tick()` or
  `fire_event()`); there is no always-running background thread. `automation`
  bridges resource degradation into job priority/deferral.
- **Phase 5 notification voice** is off by default (`channels.voice: false`).
- **Phase 6 dashboard** is local-only and authenticated by default. Remote phone
  access is disabled unless explicitly configured with TLS and read-only mode;
  push notifications are opt-in and BLOCKED-EXTERNAL until a configured local
  push provider exists. The dashboard never loads models and is stopped first
  under memory pressure.
  It only speaks when Phase 4 local voice is actually available; otherwise the
  terminal fallback is used and reported.
- Phase 7 acceptance and soak checks use local mocks. Live microphone,
  Piper/Vosk models, Ollama availability, and cloud access remain
  `BLOCKED-EXTERNAL` until verified on the target laptop.
- `files.delete` can quarantine one exact file or directory under high-risk
  approval. It moves directories with a same-filesystem rename, preserves the
  complete tree for rollback, rejects wildcard and symlink paths, and never
  recursively removes contents.
- **Phase 5 state** redacts secrets at write time. ISO timestamps are preserved
  only for known machine-readable fields so deadline arithmetic works.

## Rollback

Phase 2 storage changes are additive and reversible:
- Memory data is stored in `state/memory/` (database, files, exports, backups).
- To roll back to Phase 1, remove `state/memory/` and revert commits on `phase-2`.
- No earlier files, branches, or tags are modified.

Phase 3 storage is additive and reversible:
- Phase 3 data, drafts, quarantine and rollback plans live in `state/phase3/`.
- To roll back Phase 3, set `core_tools.enabled: false` in
  `config.local.yaml` (or remove the `core_tools` config section), then remove
  `state/phase3/`. Revert the Phase 3 commit on `phase-3` if desired.
- No Phase 1/2 file, branch or tag is deleted or rewritten. `rollback.undo`
  plans are one-shot and recorded in `state/phase3/local.db`.

Phase 4 voice is additive and reversible:
- Voice models are local files under `state/models/`; recordings and generated
  audio are temporary and removed after processing.
- Set `voice.enabled: false` in `config.local.yaml` to disable the hotkey
  listener. Revert the Phase 4 commit to remove its code and config section.
- No Phase 1-3 tag, branch or stored memory is changed.

Phase 5 automation is additive and reversible:
- All Phase 5 state is in `state/automation/` (jobs, notifications, guardian,
  build alerts, contextual reminders). Delete that directory to clear it.
- Set `automation.enabled: false` in `config.local.yaml` to stop loading the
  Phase 5 services; earlier no-op scheduler/notifier and the legacy short brief
  are restored without deleting any data.
- To remove the code, revert the Phase 5 commit on `phase-5` and delete the
  `automation:` config section. No Phase 1-4 tag, branch, memory or Phase 3
  local data is changed.

No new third-party dependencies were added in Phase 5 (standard library only).
The pinned Phase 1 `PyYAML==6.0.3` and Phase 4 voice extras are unchanged.

## Phase 7 — hardening and release

Run the standard-library suite with `python3 -m unittest discover -s tests`.
Phase 7 adds backup/restore security, audit-chain, six mocked acceptance, and
concurrent resource-soak checks. Setup, health review, secret scan, update,
backup, release, and rollback steps are in the
[Phase 7 runbook](docs/PHASE7_RUNBOOK.md). Phase 7 adds no runtime or test
dependencies and no configuration keys.
