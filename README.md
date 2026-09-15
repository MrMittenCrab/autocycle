# autocycle

macOS automation: Codex Review → Plan → Sync → Cursor ACP Implement → Checkpoint.

This revision builds on the exact local runtime snapshot at ccd5f25, including the five-stage recovery contract and the completed legacy test migration.

## Files and installed locations

| Source | Installed location |
| --- | --- |
| autocycle | ~/bin/autocycle |
| stage | ~/.autocycle/stage |
| instructions.py | ~/.autocycle/instructions.py |
| adjudication.py | ~/.autocycle/adjudication.py |
| migration.py | ~/.autocycle/migration.py |
| progress.py | ~/.autocycle/progress.py |
| sync | ~/bin/sync |
| checkpoint | ~/bin/checkpoint |

Keep these files together in a dedicated repository. Dependencies include Bash, Python 3 (SQLite), Node, Git, authenticated Codex and Cursor CLIs, and the existing network/VPN tools referenced by the scripts. macOS caffeinate is used while a run is active.

## Usage

Run commands inside the project Git repository:

```bash
autocycle 50
autocycle --instruct 'Your direction for the next new cycle'
autocycle --instructions
autocycle --cancel-instruction INSTRUCTION_ID
autocycle --stop
```

Stop finishes the current stage and preserves resume state; it does not forcibly terminate a hung provider. Resume with the original cycle-count command. Inputs submitted during a cycle wait for the next new cycle. Active inputs survive restarts and are archived only when the next opening Review verifies completion. A run that reaches its budget at Checkpoint retains review-pending state; use `autocycle <larger-total> --extend-budget` to continue. TARGET.md and IMPLEMENTATION.md are read-only to Cursor. Measured results belong in RESULT.md.

An interrupted implementation retains the original ownership baseline. If interrupted before editing files, it can resume normally. If it leaves uncheckpointed changes, AutoCycle stops before rerunning the provider: there is no trusted post-shutdown fingerprint to distinguish provider work from edits made while paused. Reconcile that work before resuming. Dirty partial work does not rewrite whether implementation originally started clean. The existing branch, HEAD, protected-document and frozen-result checks still control Checkpoint. Genuinely dirty initial baselines and old baselines already marked uncertain remain blocked; this revision does not rewrite their history or infer index cleanliness from matching file contents. Read-only Review can diagnose preserved work, but Plan still requires safely checkpointed and published work.

## Objective progress protection

The controller records work IDs, admitted step IDs, execution attempts and progress assessments in `.git/autocycle/work-state.json`. Preparing, mentioning or reserving an ID does not allocate it. Legacy migration imports headings from confirmed executed plans once; it never scans arbitrary body mentions for consumed numbers. Legacy allocation does not certify acceptance.

Plan records the original objective, starting evidence, success criterion and verification method in a single `AUTOCYCLE_PLAN` JSON record. The controller stamps its work and step identities. Retries and administrative corrections preserve those identities and original acceptance criteria. A new step is allocated when its validated plan is admitted for execution.

The following opening Review returns `AUTOCYCLE_REVIEW` alongside the existing short display fields. Review distinguishes blocking consequences from advisory findings and assesses progress against the original objective. Evidence references include the actual file content hashes and observed results. A changed file or commit alone does not establish progress. Documentation and investigation can count when they satisfy the chosen objective. Evidence interpretation remains the responsibility of the read-only reviewer; the controller verifies references, identity, ancestry and durable limits.

Two consecutive checkpointed attempts without verified progress produce `BLOCKED REPEATED_NO_PROGRESS` before another Plan. The fixed limit is two; the old `AUTOCYCLE_NO_PROGRESS_LIMIT` setting is retired. Missing evidence permits one read-only recheck, then produces `BLOCKED PROGRESS_UNKNOWN`. No extra Review is added after Checkpoint. No-change checkpoints and planning candidates are counted, and re-reviewing one attempt does not create another attempt. Previously accepted evidence cannot be alternated to earn repeated progress credit.

Restarting or extending the budget does not clear a block. Queue a concrete revised approach or the path to new evidence with `autocycle --instruct '...'`; the existing batch boundary admits it for a fresh Review. New instructions alone do not reset the guard. Review must validate new evidence or explicitly connect a revised approach to newly queued instruction IDs. Point to new ignored files or `.git/autocycle` logs through this instruction flow so they can be reviewed. Old inputs and evidence remain preserved.

## Installing this revision

Stop active AutoCycle controllers first. Run the version-checked `autocycle-progress-install.py` supplied with this revision. It checks the captured repository and installed runtime hashes, tests an isolated candidate, backs up changed files and replaces each file atomically. It preserves current repository edits, performs no commit/push, and does not run a project cycle. It refuses unrelated source changes. The old `autocycle-polish-fix.py` is retained as historical source and must not be applied to this version.

The shell runtime supports Bash 3.2 and newer. Prompt construction reads heredocs directly, avoiding the Bash 3.2 parser issue with apostrophes inside command-substitution heredocs. The installer checks the actual `/bin/bash` interpreter as well as Bash on PATH before making changes. The shell compatibility test runs real Review/Plan and checkpoint flows with literal instruction punctuation under each selected interpreter.

## Verification

```bash
bash -n autocycle
bash -n stage
bash -n sync
bash -n checkpoint
python3 test_queue.py
python3 test_current_flow.py
python3 test_flow.py
python3 test_recovery.py
python3 test_polish.py
python3 test_progress.py
python3 test_resume.py
python3 test_shell_compat.py
```

Tests use temporary Git repositories and fake providers, not real Codex/Cursor calls. Live macOS/model behavior requires local validation. The registry controls admitted step identities; Review still interprets objective evidence. Existing checkpoint branch-transition crash edges and provider liveness are not claimed to be fully solved.

Do not upload credentials, queue databases, project working files, run logs or resume-state files with this source. No such runtime state is included here.
