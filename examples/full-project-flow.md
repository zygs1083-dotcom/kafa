<!-- BEGIN GENERATED: workflow-contract:full-flow -->
# Full Local Delivery Flow

This appendix expands the same contract used by the overview, quickstart, and Skill. It is an example, not a second policy source. The schema 31 runtime is local-only; Native Codex/ChatGPT owns collaboration lifecycle and the root controller is the sole Kafa writer. `verified-patch` reuses immutable `verify run` evidence and stops before deployment or release.

## Stages

1. **Delivery plan** (`delivery-plan`): atomically create the linked local plan graph
2. **Baseline confirmation** (`baseline-confirmation`): explicitly freeze and confirm current scope
3. **Acceptance-target qualification** (`qualification`): bind acceptance revision and target digest with rationale and actor
4. **Task start** (`task-start`): root controller explicitly starts the generated planned task
5. **Task submission** (`task-submit`): root controller inspects returned code and records producer context
6. **Controller verification** (`controller-verification`): run the qualified target and persist immutable current-candidate evidence
7. **Task acceptance** (`task-accept`): accept only after submitted code and controller verification are complete
8. **Quality gate** (`quality-gate`): record reviewer findings, qualifications, and residual risk
9. **Delivery readiness** (`delivery-readiness`): reuse the canonical prerequisite evaluator before phase transition
10. **Delivery record** (`delivery-record`): record the fact-derived verified local handoff; compatibility prose flags are supplemental and deployment remains excluded
11. **Delivery validation** (`delivery-validation`): re-evaluate delivered consistency on the recorded candidate

## Required Ordering

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

## Command Skeleton

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

## Handoff

- delivered behavior and acceptance IDs
- changed files or an explicit not-derivable statement
- exact tests with counts and outcomes
- quality-gate and failure-mode status
- known gaps, not-run checks, and residual risk
- local artifact paths
- explicit statement that deployment is not included
<!-- END GENERATED: workflow-contract:full-flow -->
