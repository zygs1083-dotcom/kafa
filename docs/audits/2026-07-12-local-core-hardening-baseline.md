# Local Core Hardening Baseline

## Candidate identity

- Change: `local-core-hardening`
- Branch: `v2-local-core-slimming`
- Git HEAD/main/origin-main: `adba3691d859c0ffc93d75cc148d8f916314cc49`
- Executable workspace SHA-256: `b44236242d636c9ed01d5bd9e14dc7e81de54034d34cf28d3d52bd6eb003de70`
- Executable status SHA-256: `b3fbe64a646b2ff3fe1e5c0743c50bb358fc7ade9afd776cbe7857a3e58f3046`
- Status entry count: 180
- Schema/runtime: 30 / 5.0.0
- Candidate state: intentionally uncommitted; no commit, push, merge, tag,
  release, deploy, or active-user installation is authorized.

The executable identity covers `kafa/`, `plugins/`, `tests/`, `benchmarks/`,
`VERSION`, the two runtime Skill-eval inputs, `pyproject.toml`, and
`release.json`. OpenSpec and audit documents are outside this source digest.

## Confirmed red contracts

| ID | Finding | Required regression |
| --- | --- | --- |
| HC-MIG-1 | Normal Store operations do not coordinate with the migration sentinel/replace window | A writer active before migration is included; operations starting after announcement fail before opening SQLite |
| HC-PROJ-1 | Post-activation rollback restores SQLite but not generated views | Doctor or partial render failure restores exact pre-migration projection bytes and removes newly created paths |
| HC-TRUST-1 | High-risk evaluation receives IDs but not the gate's degraded/reviewed status | `same-context-degraded` cannot reach accepted-risk even with distinct-looking IDs and complete risk acceptance |

## Existing green baseline

- Primary strict suite: 258/258, ResourceWarning promoted to error.
- Migration/rollback matrix: 31/31.
- Adversarial matrix: 91/91.
- Real Native Codex single and parallel reports match the executable identity
  above before hardening changes.

These results establish the surrounding baseline but do not cover the three new
negative contracts. They must not be cited as proof that the findings are fixed.

## Red checkpoint

The first hardening run executed eight new tests with ResourceWarning promoted
to error. All eight failed for the intended current-candidate gaps:

- migration exclusion: three assertion failures, including a new Store
  connection opening under the sentinel and a writer entering the activation
  window;
- projection coherence: two byte/state assertion failures plus one missing
  restore-helper error;
- review status: one missing required-input error and one delivery-decision
  assertion failure where the degraded high-risk fixture returned no issue.

Five surrounding positive tests then passed: successful schema-30 activation,
all five existing failure-injection points, low/medium local trust labeling,
the reviewed-local accepted-risk path, and the schema-30 low/accepted delivery
fixtures. No production file had changed when this red checkpoint was captured.
