# Canonical Write Path Safety Baseline

Date: 2026-07-15

## Authority

- Base: `main@a24f69a1cae0f9628e4e2632c5948cbf3f366339`
- Branch: `v2-canonical-write-path-safety`
- Spec: `openspec/changes/canonical-write-path-safety/`
- Release state: `2.0.0-beta.1`, Runtime/Kernel `5.0.0`, schema `30`, `development`

## Frozen surface

| Contract | Baseline |
| --- | ---: |
| Active schema tables | 27 |
| Recursive CLI parser nodes | 53 |
| Skills | 7 |
| Hooks | 3 |
| Agent templates | 3 |
| Generated projections | 13 |

`tests.test_feature_freeze`, `tests.test_schema30_contract`, and
`tests.test_documentation_contract` passed 35/35 with `ResourceWarning` as error;
Plugin structure validation also passed. `openspec validate
canonical-write-path-safety` passed before production modification.

## Performance and size

The 5,000-fact benchmark used five samples and did not label the full suite as
run. Wall-clock values are comparative evidence; only the locked budgets are gates.

| Metric | Current baseline | Budget/status |
| --- | ---: | --- |
| Fresh DB | 315,392 B | ≤320 KiB |
| Plugin copy, caches excluded | 860 KiB (`695,552` file bytes) | ≤1 MiB |
| Fresh init median | 0.092643 s | measured |
| One mutation after 5k facts | 0.004734 s | ≤0.050 s |
| Full 13-view projection | 0.024034 s | measured |
| Targeted 3-view projection | 0.003168 s | measured |
| Strict full suite on the immediately preceding canonical baseline | 375/375 in 146.518 s | passed |

The raw benchmark report is local temporary evidence at
`/tmp/kafa-canonical-path-baseline.json`; final delivery will create a tracked
before/after audit and will not treat a missing temporary file as a pass.

## Initial risk statement

Current production has independent pathname-based entry points for Store/SQLite,
operation lock and sentinel, projections, initialization, execution artifacts,
migration/recovery, and wrapper doctor. The change therefore starts with adversarial
red tests and does not delete or replace production surfaces before those failures are
demonstrated.
