The generated block below is the maintained fresh-session contract. Live Native
profiles remain explicit, opt-in evidence and are selected only by the workflow
contract's advanced triggers.

<!-- BEGIN GENERATED: workflow-contract:skill-eval-prompts -->
# Fresh Skill Evaluation Prompts

Use a fresh context. Require exact boundary language and refuse to treat skipped, blocked, not-run, fixture-only, or zero-count evidence as pass.

## Route Checks

- Confirm `project-harness` routes work described as: broad, architectural, cross-module, long-lived, or complete verified delivery work
- Confirm `minimal-safe-change` routes work described as: small clear low-risk patch with explicit acceptance
- Confirm `bug-fix-loop` routes work described as: reproducible defect or failing behavior
- Confirm `test-first-delivery` routes work described as: contract-sensitive or regression-sensitive behavior
- Confirm `independent-quality-gate` routes work described as: finished implementation needs fresh review
- Confirm `harness-audit` routes work described as: runtime, boundary, fact, or generated-view drift requires audit
- Confirm `project-retrospective` routes work described as: a completed milestone or repeated escape needs lessons captured

## Advanced Trigger Scenarios

- Small single-producer work: expect no advanced trigger and do not load the full delegation matrix.
- Scenario `parallel-delegation`: when two or more producers run in parallel, shared-file integration is required, or explicit advanced review is requested; expect `parallel-delegation` and full delegation matrix and root integration checkpoint.
- Scenario `deep-kernel-review`: when schema, migration, runtime ownership, trust, delivery gate, security, permissions, concurrency, data loss, public API, or cross-module authority changes; expect `deep-kernel-review` and root/deep ownership and adversarial review.
- Scenario `harness-audit`: when multi-day work, repeated escapes, schema or runtime change or drift, or milestone review; expect `harness-audit` and harness-audit.
- Scenario `project-retrospective`: when delivery milestone completes or a failure loop exposes a stable lesson; expect `project-retrospective` and project-retrospective.
- Scenario `live-host-compatibility`: when Native Host integration, evaluator, packaging, or release surface changes; expect `live-host-compatibility` and real Native single and parallel evidence.
- Scenario `release-rehearsal`: when packaging, supply-chain, release tooling, or an authorized release candidate changes; expect `release-rehearsal` and non-publishing isolated rehearsal.

## Dependency Checks

- `delivery-plan` → `baseline-confirmation`
- `delivery-plan` → `qualification`
- `delivery-plan` → `task-start`
- `task-start` → `task-submit`
- `qualification` → `controller-verification`
- `task-submit` → `task-accept`
- `controller-verification` → `task-accept`
- `task-accept` → `quality-gate`
- `baseline-confirmation` → `delivery-readiness`
- `quality-gate` → `delivery-readiness`
- `delivery-readiness` → `delivery-record`
- `delivery-record` → `delivery-validation`

## Command Checks

```bash
kafa project init --repo .
kafa project quickstart --repo . status
kafa project quickstart --repo . delivery-plan --file delivery-plan.json --json
kafa project baseline --repo . confirm --id BL1 --summary 'confirmed scope' --by root-controller
kafa project task --repo . start PATCH-T1
kafa project quickstart --repo . verified-patch --id PATCH --json
kafa project task --repo . submit PATCH-T1 --context-id producer-context --evidence 'root inspected returned code'
kafa project task --repo . accept PATCH-T1 --evidence 'verification and review complete'
kafa project gate --repo . record --reviewer-context fresh --reviewer-context-id reviewer-context --result pass --qualification PATCH-Q1
kafa project delivery --repo . ready
kafa project delivery --repo . record --scope 'verified local handoff' --handoff 'return code and residual risks'
kafa project validate --repo . --delivery
```

## Handoff Checks

- delivered behavior and acceptance IDs
- changed files or an explicit not-derivable statement
- exact tests with counts and outcomes
- quality-gate and failure-mode status
- known gaps, not-run checks, and residual risk
- local artifact paths
- explicit statement that deployment is not included

## Result Contract

A live Host evaluator must return `source: host-evaluated` followed by the exact ordered contract lines and closed `scenario-verdict` records; no extra prose, unknown scenario, contradiction, or fixture source is accepted. The generated local transcript uses `source: fixture-only` and is never fresh Host evidence.

High/critical work without independent current-candidate provenance must remain `human-review-required`.
<!-- END GENERATED: workflow-contract:skill-eval-prompts -->
