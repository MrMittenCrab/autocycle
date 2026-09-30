## Testing

This file is the sole authority for choosing test scope. Use minimum-sufficient, dependency-triggered verification:

1. **Focused verification:** the smallest test(s) proving the changed behavior.
2. **Affected-regression verification:** additional tests only when a changed shared dependency or invariant gives a concrete reason to broaden.
3. **Full certification:** the complete deterministic AutoCycle suite, for explicit certification/release or another explicitly justified repository-wide invariant.

Executable-code changes and ordinary development work do not automatically require full certification. Before running a broad/full suite, state the concrete reason focused/affected verification is insufficient. Select tests explicitly with `python3 run_tests.py test_install.py` (or multiple names/paths); no selection means full certification.

`python3 install.py --install` explicitly requests certified installation and retains its full-suite gate. `python3 install.py --install-verified` assumes the caller already completed verification required here; it does not certify or independently widen test scope. Both preserve non-test installation safeguards.

- During a task, run a test only when:
  1. source, configuration, or a dependency relevant to that test has changed since the applicable passing result; or
  2. the test is needed to diagnose a current failure.
- Start with the smallest focused test or test group that can establish the changed behavior.
- After a failure, rerun the failing test first.
- Broaden testing only when evidence indicates that a shared dependency or wider invariant may be affected.
- Do not rerun already-passing unaffected tests merely for reassurance.
- Reuse a prior passing result only when its relevant source and dependencies are demonstrably unchanged and the result still applies to the current repository state.
- Prefer deterministic synthetic tests over live/native integration tests when they establish the same invariant.
- For Observer / Office Bridge work, use synthetic Word/Excel fixtures by default. Run live Word or Excel only when the changed native behavior cannot be established synthetically.
- Do not run BAV or another target project merely to validate AutoCycle unless the task explicitly requires end-to-end target-project validation.
- If relevant source or dependencies change after verification, repeat the affected checks. Repeat full certification only if an explicit certification/release gate or stated repository-wide invariant still requires it.
- Do not impose an arbitrary numerical test or time cap. Minimize testing by dependency and evidentiary relevance.
- Before launching a broad suite during development, there must be a concrete reason why focused verification is insufficient.

The governing principle is: run the smallest set of tests that can materially change confidence in the current change, while preserving explicitly requested certification gates and non-test installation safeguards.
