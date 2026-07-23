<!-- BEGIN GENERATED: workflow-contract:trigger-matrix -->
# Trigger Matrix

| Skill | Trigger | Obligation |
| --- | --- | --- |
| `project-harness` | broad, architectural, cross-module, long-lived, or complete verified delivery work | route to OpenSpec when specification is needed, then run the complete local delivery workflow |
| `minimal-safe-change` | small clear low-risk patch with explicit acceptance | keep the diff and evidence surface narrow |
| `bug-fix-loop` | reproducible defect or failing behavior | reproduce before fixing and retain a regression oracle |
| `test-first-delivery` | contract-sensitive or regression-sensitive behavior | establish the failing test before production change |
| `independent-quality-gate` | finished implementation needs fresh review | keep producer and reviewer contexts distinct when independent review is claimed |
| `harness-audit` | runtime, boundary, fact, or generated-view drift requires audit | audit evidence without relabelling missing checks as pass |
| `project-retrospective` | a completed milestone or repeated escape needs lessons captured | derive lessons from verified delivery evidence |

## Advanced Modes

| Advanced mode | Trigger | Activates |
| --- | --- | --- |
| `parallel-delegation` | two or more producers run in parallel, shared-file integration is required, or explicit advanced review is requested | full delegation matrix and root integration checkpoint |
| `deep-kernel-review` | schema, migration, runtime ownership, trust, delivery gate, security, permissions, concurrency, data loss, public API, or cross-module authority changes | root/deep ownership and adversarial review |
| `harness-audit` | multi-day work, repeated escapes, schema or runtime change or drift, or milestone review | harness-audit |
| `project-retrospective` | delivery milestone completes or a failure loop exposes a stable lesson | project-retrospective |
| `live-host-compatibility` | Native Host integration, evaluator, packaging, or release surface changes | real Native single and parallel evidence |
| `release-rehearsal` | packaging, supply-chain, release tooling, or an authorized release candidate changes | non-publishing isolated rehearsal |

A user's explicit request for a named advanced mode also activates it. Explanation, translation, and supplied-text summary do not initialize Kafa. Deployment, production release, external SaaS actions, and Native Host lifecycle remain outside the local runtime.
<!-- END GENERATED: workflow-contract:trigger-matrix -->
