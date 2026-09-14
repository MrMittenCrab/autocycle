# autocycle

macOS automation: Codex Review → Plan → Sync → Cursor ACP Implement → Checkpoint.

This source snapshot combines the September 14 exported scripts with the instruction queue, compact UI, timer cleanup and step-index guidance patches. It is a reconstructed source snapshot, not a fresh export from the running Mac.

## Files and installed locations

| Source | Installed location |
| --- | --- |
| autocycle | ~/bin/autocycle |
| stage | ~/.autocycle/stage |
| instructions.py | ~/.autocycle/instructions.py |
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

Stop finishes the current stage and preserves resume state; it does not forcibly terminate a hung provider. Resume with the original cycle-count command. Inputs submitted during a cycle wait for the next new cycle. Active inputs survive restarts and are archived at the verified checkpoint. TARGET.md and IMPLEMENTATION.md are read-only to Cursor. Measured results belong in RESULT.md.

## Applying the latest small patch

For an existing queue-enabled installation, stop autocycle, wait for Paused and run `python3 autocycle-polish-fix.py`. It changes only stage spacing and step-index prompts, preserving other installed content. It backs up both scripts. Do not restore older scripts over newer queue state.

## Verification

```bash
bash -n autocycle
bash -n stage
bash -n sync
bash -n checkpoint
python3 test_queue.py
python3 test_flow.py
python3 test_recovery.py
python3 test_polish.py
```

Tests use temporary Git repositories and fake providers, not real Codex/Cursor calls. Live macOS/model behavior requires local validation. Step numbering is guided by prompts and recent history, not guaranteed by a numeric parser. Existing checkpoint branch-transition crash edges and provider liveness are not claimed to be fully solved.

Do not upload credentials, queue databases, project working files, run logs or resume-state files with this source. No such runtime state is included here.
