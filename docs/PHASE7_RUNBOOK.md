# Phase 7 release runbook

This release hardens the existing Phases 1–6. It adds no runtime dependencies,
environment variables, configuration keys, or assistant tools. The release
branch is `phase-7`; do not merge it into `main` as part of this runbook.

## Setup and startup

1. Use Python 3.11 or newer and install the pinned project dependency from
   `pyproject.toml` (`PyYAML==6.0.3`). The tests use the standard library's
   `unittest` runner; pytest and a virtual environment are not required.
2. Copy `.env.example` to `.env` only if needed. Leave cloud keys empty when
   cloud is disabled. Keep real keys in `.env` or the OS keychain.
3. Keep personal overrides in the git-ignored `config.local.yaml`.
4. Start terminal Omen with `python3 -m omen.cli.main`. Use `/health` and
   `/status` before enabling optional services.

## Checks before release

Run the full suite from the repository root:

    python3 -m unittest discover -s tests

Run the concurrent soak for one minute:

    OMEN_PHASE7_SOAK_SECONDS=60 python3 -m unittest tests.test_phase7.TestPhase7ResourceSoak -v

The soak starts chat, mocked voice, persistent memory indexing, and scheduled
notification work together. It checks errors, audit integrity, process peak RSS
against the 8 GiB ceiling, and the model manager estimate against 9 GiB. The
default suite run uses a short soak; use the one-minute command for release
evidence.

Run the built-in health command in Omen:

    /health
    /status

The health check verifies config loading, configured provider adapters, tool
registration, safety wiring, audit-chain integrity, resources, memory, and
budget state. Provider health is configuration-only: adapters do not probe
reachability until a real completion.

## Security and audit review

- Tool writes remain permission-classed and pass policy, exact approval, and
  audit checks. The acceptance suite checks exact-path binding, single-use
  approval, kill-switch cancellation, and post-kill locking.
- Audit records are redacted and hash-chained. Appends now validate the existing
  chain while holding the file lock, so concurrent instances share one chain
  and a modified log rejects further writes. `verify_integrity()` reports a
  damaged chain as unhealthy.
- Audit payload redaction covers token and resource fields as well as args,
  results, approvals, errors, and summaries. Audit files use mode `0600` and
  their directory uses `0700`.
- Memory archives use an SQLite online snapshot, are published without
  overwriting an existing archive, and use file mode `0600` in a `0700`
  directory. Restore accepts only regular files/directories in the expected
  archive layout, rejects links and traversal paths, and writes files atomically.
- Secret scan: inspect tracked source/config/docs for private-key headers,
  non-placeholder provider tokens, and populated `OMEN_*KEY`/`OMEN_*TOKEN`
  assignments. Test fixtures deliberately contain fake credentials and must
  never be treated as real secrets. `.env` and `config.local.yaml` are ignored.

## Backup, update, and rollback

Before updating, stop Omen with `/bye`, create a memory backup with
`/backup-memory`, then make a private snapshot of the stopped application's
state directory. `state/` can contain memory, local records, automation state,
cost data, and logs; the memory archive alone does not include all of these.

    umask 077
    tar -czf "$HOME/omen-state-before-update.tar.gz" state

Retain `.env` and `config.local.yaml` separately in a protected location; never
put them in a shared archive. Verify the memory archive exists and has mode
`0600` before proceeding.

Updates are explicit Git fast-forwards from a reviewed release tag. There is no
background or automatic updater:

    git status --short --branch
    git switch phase-7
    git fetch --tags origin
    git merge --ff-only omen-phase-7-complete
    python3 -m unittest discover -s tests

If the release check fails, stop Omen and revert the Phase 7 release commit on
`phase-7` with `git revert --no-edit <release-commit>`, then rerun the suite.
Phase 7 has no storage schema migration, so reverting code leaves user state
untouched. Do not reset or clean the worktree to roll back.

To restore only a memory archive, stop Omen and first preserve the current
`state/` snapshot. Then use the existing file-store restore API:

    python3 - <<'PY'
    from pathlib import Path
    from omen.memory import OmenMemoryStore

    store = OmenMemoryStore(db_path="state/memory/omen_memory.db")
    store.close()
    store.filestore.restore_backup_archive(
        Path("/protected/path/omen_memory_backup.tar.gz"),
        Path("state/memory/omen_memory.db"),
    )
    PY

Restart Omen and check `/health`, `/memory`, and `/history`. Preserve the
pre-restore snapshot until the restored data is verified.

## Acceptance evidence and limits

The `tests.test_phase7.TestPhase7Acceptance` tests use deterministic local
adapters and mocked audio. They exercise:

1. Voice transcript -> reminder approval -> local reminder -> mocked spoken
   alert.
2. Exact-path risky directory deletion -> approval -> quarantine ->
   plan/approval/execution/verification audit evidence; changing the path
   requests new approval. Rollback restores the complete directory tree.
3. Preference persistence across an app restart -> preference recall -> forget
   -> absent from export.
4. Warning/degrade/emergency ordering -> dashboard/job/index/model/voice
   shedding -> clean resource recovery.
5. Kill during mocked speech with a pending tool approval -> speech stops,
   pending approval is cancelled, later tool calls are blocked, and kill is
   audited.
6. Exhausted cloud budget -> local provider fallback -> user notice and audit.

`files.delete` moves exact-path directories into quarantine by same-filesystem
rename. It does not traverse or remove contents recursively; wildcard and
symlink paths are rejected. Cross-filesystem directory moves fail without
copying or deleting the tree. Shell `rm` and `rmdir` remain denied.

Real microphone/speaker latency, Piper/Vosk model loading, wake-key hardware,
and Ollama/cloud provider reachability were not measured by mocked tests. These
remain `BLOCKED-EXTERNAL` until checked on the target laptop. Current test
fixtures verify transcript flow, cancellation, fallback, and latency guards;
they do not establish hardware performance targets.

## Release record

Record the test output, commit hash, and `omen-phase-7-complete` tag after all
release checks pass. Keep the branch on `phase-7`; do not merge it into `main`.
