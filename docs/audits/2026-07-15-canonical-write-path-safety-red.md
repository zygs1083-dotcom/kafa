# Canonical Write Path Safety Red Evidence

Date: 2026-07-15

Candidate: `v2-canonical-write-path-safety@0facd65` plus the uncommitted red-test suite only. No production file had been changed when this evidence was captured.

## Red command

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -c 'import sys,unittest,warnings; warnings.simplefilter("error", ResourceWarning); s=unittest.defaultTestLoader.loadTestsFromName("tests.test_project_fs_safety"); r=unittest.TestResult(); s.run(r); print(f"run={r.testsRun} failures={len(r.failures)} errors={len(r.errors)} skipped={len(r.skipped)}"); sys.exit(0 if r.wasSuccessful() else 1)'
```

Final pre-production result: `run=36 failures=47 errors=5 skipped=1`, exit 1. The five errors are the intentionally absent `core.project_fs` contract. The single skip is only the macOS-inapplicable Windows junction case; it is not reported as passing. POSIX symlink, hard-link, FIFO, socket and deterministic Event-coordinated exchange cases executed locally.

The failures demonstrate these unchanged-production defects:

- init follows or silently accepts unsafe `.gitignore`, all 13 projections, retired evidence and all three Native agent-template destinations after beginning runtime mutation;
- Store opens linked or hard-linked DB authority, follows linked operation-lock and migration-sentinel authority, and reaches SQLite for unsafe WAL/SHM/journal and backup targets instead of returning the stable path error;
- local and container executors accept linked stdout destinations, and local structured-result parsing follows an external source;
- complete local/container verification can persist passing execution and validation facts through linked stdout or structured-result authority; a same-content symlink exchange between the two validation passes is also accepted;
- migration and recovery do not uniformly reject linked backup roots, staging DBs, manifests, projection backup/restore paths, failed DBs, sidecar destinations, restore temporaries and sentinels;
- a linked manifest temporary can permit activation, while a linked restore temporary after activation can be treated as a completed rollback instead of retaining `rollback-incomplete` recovery authority;
- the closed relative grammar, root-alias pinning, non-regular rejection and identity-change seam do not exist yet.

Every external referent assertion records bytes, SHA-256, mode and inode where applicable. Event-coordinated race tests use no correctness sleep.

## Existing positive contract

```bash
PYTHONWARNINGS=error PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -q \
  tests.test_local_core_hardening \
  tests.test_execution_validation \
  tests.test_structured_test_results \
  tests.test_sandbox_execution
```

Result: 37/37 passed in 12.598 seconds. This confirms the pre-change migration, locking and execution positive behavior remains intact while the new adversarial contract is red.

## Interpretation

This is deliberately failing evidence. It is not a pass, and it does not claim Windows junction/reparse execution on macOS. The next acceptable state is to make the same adversarial suite green through the internal `ProjectFS` seam without weakening the existing 37-test positive contract.
