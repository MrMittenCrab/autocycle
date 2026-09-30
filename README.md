# AutoCycle

AutoCycle orchestrates work on a target Git project such as BAV. It keeps a durable Session, admits bounded implementation work, and returns each result to independent Review.

```text
BAV / target project
        ↑
AutoCycle
  ├── Controller
  ├── Reviewer
  ├── Planner
  ├── Implementer
  └── Observer
       └── Office Bridge implementation
```

| Component | Authority | Does not own |
|---|---|---|
| Controller | Lifecycle, durable state, transitions, output admission, instructions, ownership, access, capabilities, retries, recovery, clocks, Baseline, Checkpoint, retention | Semantic truth, acceptance, or implementation strategy |
| Reviewer | Material requirements, relevance, evidence sufficiency and aggregation, Completion and Session Endpoint judgments | Product edits, implementation mechanics, or Controller protocols |
| Planner | Smallest useful supported route from unresolved material findings, respecting Session Priority and dependencies | Final acceptance or invented capabilities; does not call Observer |
| Implementer | Execution of the admitted Plan, product changes, supported checks, measured results | Redefining acceptance or certifying its own final acceptance |
| Observer | Authenticated native/external observations, actions, images, receipts and diagnostics | Semantic acceptance or workflow advancement |

There are exactly three recurring stages:

```text
Review → Plan → Implement → next Review
```

Controller governs transitions. A cycle allowance limits work; exhausting it does not declare the Session complete. Reviewer alone decides whether the Endpoint has been reached. A cycle can finish after Review or Plan without inventing later stages.

## Observation

Office Bridge implements Observer for opted-in Word and Excel projects. Reviewer may request supported read-only inspection of checkpointed artifacts. Implementer may request observations that help execute the current admitted Plan. Observer returns to the same caller, cycle and work identity; its time belongs to that stage.

```text
Review    → Observe Word / Observe Excel → same Review
Implement → Observe Word / Observe Excel → same Implement
```

A provider requests an observation with a single `OBSERVER_REQUEST` response containing a finite `requests` array. Each request identifies an enabled app, source and actual source SHA-256; Excel also requires a worksheet. Existing supported capture/navigation fields remain available. Controller authenticates the request and project boundary, dispatches Observer, and continues the caller. An observation request is not a final stage result.

Reviewer first considers applicable existing evidence and supported read-only observation. It may reuse Implement observations when the relevant final artifact and dependencies remain unchanged. Later Review time alone does not invalidate evidence. Changed sources, missing retained artifacts, mismatched hashes or ownership cannot establish acceptance. Capture success alone never establishes readability or other semantic facts.

Native interaction uses owned fixed Office slots. User documents and immutable evidence are not opened for mutation. Requests, receipts, screenshots and source bindings remain authenticated. Capture failures are technical observations, not automatic product defects, human blockers or capability absence.

New work never writes `NATIVE_HANDOFF` or `native-handoff.json`. Controller retains an authenticated reader for saved legacy handoffs. Older deferred observation associations migrate once to ordinary Review with provenance and prior substantive blocks retained; they do not automatically execute an old request. Interrupted current calls retain their exact caller and snapshot in `observer-call.json`; continuation validates those bindings before returning to that caller.

## Timing and output

Access runs once per process invocation, including a fresh `--resume`, when the project requires an Office access check. It is outside Cycle.

```text
Access      ✓ 00:08

Cycle       18/50

Review      ✓ 05:44
    ✓ Observe Word
    ✓ Found   Required issue remains
    ✓ Pruned  2 files · 100 B
Plan        ✓ 01:12
    ✓ Planned Step 1.18 — Bounded repair
    ✓ Baseline abc1234
Implement   ✓ 08:31
    ✓ Modify publication
    ✓ Run tests
    ✓ Checkpoint def5678
Cycle          15:27
```

Controller owns one identity and one active clock for each entered stage. Model calls, corrections, Controller validation and persistence, Observer calls and continuation all share it. Review includes pruning; Plan includes Baseline; Implement includes Checkpoint. Renderer restarts do not restart the clock.

```text
Cycle = Review + Plan + Implement
```

This equality always applies to displayed active durations, including cycles with interruptions. For early exits, only entered stages contribute. Cycle measures active cycle work, not elapsed wall time. Access and Network are excluded. Run wall time additionally includes Access, Network and startup/exit overhead.

Network is a standalone interruption record, not a component, stage or subphase. A genuine transport interruption pauses the current stage clock. Its own timer updates in place, with one blank line before and after the unindented row. Recovery resumes the same stage; the completed Network block remains in chronological position. Each distinct interruption has its own block.

```text
Implement   ✓ 06:04
    ✓ Modify publication
    ✓ Observe Word

Network     ✓ 00:31

    ✓ Run tests
    ✓ Checkpoint abc1234
```

Retry exhaustion marks the current Network record `✗ … retry exhausted` and preserves interrupted work. It does not turn transport failure into a substantive failed Review. Semantic/model errors, invalid responses, Office errors, failed project tests, access failures and capability gaps are not Network.

Git publication retries use the same interruption accounting and push an immutable commit identity. Bounded provider-capacity retries preserve partial-work safeguards but remain active Implement time; they are not Network interruptions.

Non-TTY output is append-only and contains no cursor controls. An interrupted stage prints its header once, Network start/outcome records, then a `✓ Review active MM:SS` (or the corresponding caller/status) continuation result. Active stage duration remains separate from Network duration.

## Authenticated boundaries

There are two implementation boundaries:

1. **Baseline**, under Plan: the exact published Planner-approved commit, branch and clean project snapshot from which Implementer is authorized to work. Controller synchronization pins that commit and branch.
2. **Checkpoint**, under Implement: the exact durable implementation result for the next Reviewer. Controller rechecks ownership and frozen result files around staging and verifies publication.

Dirty initial worktrees, changed protected documents, changed indexes, unknown partial work and mismatched recovery receipts fail closed. `TARGET.md`, `SESSION.md` and `IMPLEMENTATION.md` are read-only to Implementer. A provider completion marker does not authorize acceptance or bypass Checkpoint safety.

Reviewer states unresolved material facts. Planner supplies finite supported routes, preferring valid existing evidence before new observations or product changes. Controller checks executability before Implement. An unsupported material route stops with a verification-capability gap; an available mechanism that fails at runtime remains a technical failure. New `unclassified` requirements, prose-derived execution state and generic matching-receipt acceptance are rejected.

## Project documents and state

- `TARGET.md`: project direction.
- `SESSION.md`: current Endpoint and Priority.
- `IMPLEMENTATION.md`: the current admitted bounded Plan and Completion.
- `RESULT.md`: measured implementation results when needed.

Controller state lives in the target project's `.git/autocycle/`:

| File | Purpose |
|---|---|
| `resume-state` | Session, cycle budget and durable routing boundary |
| `work-state.json` | Work/attempt identities, Review assessments, admitted routes and observation references |
| `instructions.sqlite3` | Immutable instruction delivery batches and receipts |
| `current-review` | Authenticated current Review decision |
| `planned-commit` | Exact Plan publication identity retained across interruption before Baseline |
| `implementation-baseline.json` | Exact initial ownership snapshot |
| `implementation-result.json` | Frozen provider result for Checkpoint |
| `implementation-interruption.json` | Single-use authenticated transport recovery |
| `observer-call.json` | Pending/returned same-stage observation and caller snapshot |
| `office/requests`, `office/receipts`, `office/evidence` | Authenticated Observer inputs, outcomes and retained artifacts |

Display files and terminal anchors are ephemeral presentation/accounting data. They never establish project truth, acceptance or workflow authority. Controller retains supported saved-state migration readers; historical evidence is not rewritten to manufacture acceptance.

## Commands

Run commands inside the target Git repository:

```sh
autocycle 5
autocycle --resume
autocycle --extend 5
autocycle 5 --restart --instruct 'Endpoint: deliver the next capability'
autocycle --stop
autocycle --instruct 'Prioritize the end-to-end workflow'
autocycle --instructions
autocycle --cancel-instruction <id>
autocycle 1 --dry-run
```

`--resume` keeps the saved budget and work identity. `--extend` adds cycles. `--stop` requests a pause after the current checkpoint and bookkeeping. An unfinished Session requires resume, extension or explicit restart. Restart preserves owned partial work and history before starting a new Session. Instruction delivery alone does not prove project completion.

For native Office, opt in through `.autocycle.toml`:

```toml
[capabilities]
native_office = ["word", "excel"]
```

Native Office requires macOS, the enabled applications and their permissions. Tests normally use synthetic Office fixtures. `check_word_native.py` and `check_excel_native.py` are explicit live diagnostics, outside the deterministic suite.

## Source and installation

| Owner | Canonical files |
|---|---|
| Controller | `autocycle`, orchestration/admission in `stage`, `adjudication.py`, `progress.py`, `instructions.py`, `migration.py`, `remote_docs.py`, `sync`, `checkpoint`, `stage_display.js`, `implementation_response.js` |
| Reviewer | Review prompt contract in `stage` |
| Planner | Plan prompt contract in `stage` |
| Implementer | Implement prompt contract in `stage` |
| Observer | `native_office.py`, `excel_verification.py`, `office_capture.swift` |

Physical files need not correspond one-to-one with components. Validators enforce structure, provenance and executability; provider contracts retain the separate semantic and execution authorities.

```sh
python3 run_tests.py       # isolated deterministic Git/provider/Office fixtures
python3 install.py --install
```

The installer checks that controllers are stopped, compiles the native helper, runs the deterministic suite, verifies source stability and installs atomically with backups. `autocycle`, `sync` and `checkpoint` install to `~/bin`; other runtime files and the compiled `autocycle-office-capture` helper install to `~/.autocycle`. Installation does not start a project.

Requires Bash 3.2 or newer, Python 3.11 or newer, Node.js, Git, authenticated Codex and Cursor providers; native capture builds with Apple's Swift tools. Local repair reports, install manifests, caches and diagnostic logs are verification artifacts, not canonical source. Supported installation uses `install.py`, not historical patch installers.
