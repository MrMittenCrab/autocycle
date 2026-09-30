## Testing

Use minimum-sufficient, dependency-triggered verification.

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
- Run the complete deterministic AutoCycle suite once at the final verification gate when the task changes shared/executable runtime behavior or when installation/release requires that gate.
- Documentation-only, instruction-only, or otherwise non-executable changes do not by themselves require the complete deterministic suite.
- If relevant executable code changes after a final complete-suite pass, run the affected focused tests first, then run the complete deterministic suite once more if that final gate is still required.
- Do not impose an arbitrary numerical test or time cap. Minimize testing by dependency and evidentiary relevance.
- Before launching a broad suite during development, there must be a concrete reason why focused verification is insufficient.

The governing principle is: run the smallest set of tests that can materially change confidence in the current change, while preserving required final release/install gates.
