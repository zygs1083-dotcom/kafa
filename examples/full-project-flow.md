# Full Local Project Flow

This example shows one end-to-end local-only path from a substantial feature
request to a verified local code handoff. OpenSpec owns the specification, Native
Codex/ChatGPT owns the work lifecycle, and Kafa records only local delivery
facts. The flow stops before deployment or release.

## Example Request

```text
我要开发一个微信小程序，用于管理亲友关系、生日提醒和关系图谱。
```

## 1. Inspect the Real Workspace

The root controller first reads applicable `AGENTS.md` files and project entry
documents, then inspects the repository root, current branch/revision, remotes,
and dirty state. Existing user changes are preserved.

Expected summary:

```text
目标是交付一个可运行的亲友关系管理小程序代码候选，包含亲友档案、生日提醒和
基础关系图。该范围跨多个模块并具有长期行为约束，因此先由 OpenSpec 固定规格，
再由 Kafa 对本地候选执行可验证交付。部署和发布不在本次范围内。
```

The root controller does not initialize Git, create a branch, commit, or push
unless the user authorized that action.

## 2. Make OpenSpec the Specification Authority

Because the request is broad and cross-module, create or select an OpenSpec
change such as `family-mini-program`. The change owns:

- `proposal.md`: problem, goals, non-goals, and impact;
- `design.md`: architecture and locked decisions;
- `specs/**/spec.md`: observable requirements and scenarios; and
- `tasks.md`: dependency-ordered implementation checklist.

Before implementation, read those files in order and validate the change:

```bash
openspec status --change family-mini-program
openspec validate family-mini-program
```

Do not copy the OpenSpec documents into Kafa as a second specification. Record
only stable IDs and local acceptance/task facts needed for verification. When
`tasks.md` is the unique implementation checklist, follow its dependency order
and update each checkbox immediately after its evidence is verified.

## 3. Initialize the Local Runtime

For an ordinary project, first verify that the installed Plugin is discoverable:

```bash
kafa project doctor --repo .
```

The remaining examples use the runtime resolved by the installed
`project-harness` Skill. In a source checkout, a convenient shell helper is:

```bash
KAFA_PLUGIN_ROOT=/absolute/path/to/kafa/plugins/codex-project-harness
harness() {
  python3 "$KAFA_PLUGIN_ROOT/scripts/harness.py" --root . "$@"
}
```

Initialize and inspect the project-local facts:

```bash
harness init
harness status
harness quickstart status
```

Initialization creates `.ai-team/state/harness.db` and local projections. It
does not request external credentials, create a remote project, or start a
worker process.

## 4. Record the Minimal Delivery Baseline

Suppose OpenSpec defines:

```text
REQ1  A user can create and edit a relative profile.
AC1   A saved profile can be reopened with the same name and birthday.
T1    Implement the profile model, storage, and tests.
```

The root controller records those local verification facts:

```bash
harness requirement add \
  --id REQ1 \
  --kind functional \
  --body "A user can create and edit a relative profile" \
  --priority must

harness acceptance add \
  --id AC1 \
  --criterion "A saved profile reopens with the same name and birthday" \
  --priority must

harness requirement link --requirement REQ1 --acceptance AC1
harness baseline freeze --id BASE-1 --summary "OpenSpec family-mini-program baseline"

harness task add \
  --id T1 \
  --task "Implement the profile model, storage, and tests" \
  --owner developer \
  --acceptance AC1
```

These rows support traceability and delivery checks. OpenSpec still owns the
full product language, design, and task checklist.

For data loss, permissions, concurrency, migrations, destructive behavior, or
other meaningful risk, record explicit failure modes before implementation.
High/critical work follows the stricter review rule described below.

## 5. Let the Native Host Own Implementation

The root controller starts the local task intent:

```bash
harness task start T1
```

Native Codex/ChatGPT then creates any visible task, subagent, or worktree needed
for implementation. Kafa does not create or merge that worktree, choose the
model, manage approval, cancel the worker, or collect hidden worker output.

A bounded developer returns:

```text
Changed files:
- src/profile.py
- tests/test_profile.py

Checks run:
- focused unit test during implementation

Remaining risk:
- persistence failure path still needs root-controller verification
```

The worker does not mutate `.ai-team/state/harness.db`. The root controller
reviews the returned change in the target workspace.

## 6. Establish the Current Candidate

Before trusted verification, make the candidate stable and inspectable:

- In Git, use an existing clean revision or a user-authorized commit.
- Without Git, Kafa uses local content identity.
- Never create a passing quality gate on a dirty Git worktree.
- Never commit merely to satisfy Kafa unless the user authorized committing.

If the candidate changes after verification, run verification again. Historical
results remain auditable but do not satisfy the new candidate.

## 7. Run Controller Verification

Register the exact test target and link it to the task:

```bash
harness test-target add \
  --id PROFILE-UNIT \
  --kind unit \
  --command-template "python3 -B -m unittest discover -s tests -p 'test_*.py'" \
  --result-format regex

harness test-target link --task T1 --target PROFILE-UNIT
```

The root controller, not the implementation worker, executes it:

```bash
harness verify run --target PROFILE-UNIT --acceptance AC1
```

A passing run records one immutable execution with the current candidate,
command, exit code, positive test count, semantic result, stdout artifact and
digest, runner policy, and validation link. A manual statement such as "tests
passed" cannot replace this execution.

Missing or malformed structured output, zero executed tests, a stale candidate,
artifact digest mismatch, or an unsatisfied sandbox/no-network policy fails
closed.

After implementation evidence has returned to the root controller:

```bash
harness task submit T1 \
  --context-id native-producer-context \
  --evidence "candidate and controller verification ready for independent review"
```

The context identifier is procedural audit metadata, not proof of host identity.

## 8. Perform Independent Review

The native host starts a short-lived `qa-reviewer` context distinct from the
producer. The reviewer checks:

- behavior against OpenSpec scenarios and `AC1`;
- logic errors and simpler alternatives;
- data-loss and error paths;
- current-candidate test evidence;
- dirty-tree and stale-candidate hazards; and
- any open finding or unverified claim.

The reviewer returns concrete findings and the checks actually inspected. The
root controller records any material finding, resolves blockers, and accepts the
task only after review:

```bash
harness task accept T1 --evidence "independent QA accepted AC1 on the current candidate"

harness gate record \
  --reviewer-context fresh \
  --reviewer-context-id native-reviewer-context \
  --result pass
```

For low/medium work, a same-context review must be labeled
`same-context-degraded`; it must not be called fresh.

For high/critical work, current structured execution and distinct context
metadata are necessary but not sufficient for autonomous delivery. Without
verifiable provenance, Kafa returns `human-review-required`. Delivery may
continue only if the user explicitly accepts or exempts every remaining risk
with actor, reason, scope, current revision, and unexpired expiry. That path is
reported as procedural accepted risk, never cryptographic proof.

## 9. Record Verified Handoff

After the gate passes and all delivery prerequisites are current, record the
local handoff:

```bash
harness delivery record \
  --scope "Relative profile slice from family-mini-program" \
  --acceptance AC1 \
  --changed-files "src/profile.py,tests/test_profile.py" \
  --validation "PROFILE-UNIT passed with a positive executed-test count" \
  --qa "independent qa-reviewer context accepted AC1" \
  --quality-gate pass \
  --known-gaps "birthday reminders and relationship graph remain in later tasks" \
  --handoff "verified local code candidate; no deployment performed"

harness validate --delivery
harness status
```

The final user-facing handoff reports:

- delivered behavior and OpenSpec acceptance mapping;
- current candidate identity and changed files;
- exact checks run, test counts, and outcomes;
- independent review and quality-gate result;
- failure-mode coverage or complete accepted-risk metadata;
- data/config/migration implications;
- local artifact paths;
- known gaps, not-run checks, and residual risk; and
- an explicit statement that deployment and release were not performed.

`skipped`, `blocked`, `not-run`, unavailable, and fixture-only checks remain
listed as such. They are not converted into passes.

## 10. Recovery and Audit

Normal changes update only affected local projections. If a generated view is
missing or damaged:

```bash
harness projection rebuild
harness doctor
```

Compact audit events explain local mutations but are not a replay source.
Migration and administrator repair use verified SQLite backups. Inspect a repair
plan before applying it:

```bash
harness repair --dry-run
```

Schema migration creates and verifies a backup before activating schema 30. If
activation validation fails, Kafa restores that backup rather than rebuilding
state from events.

## Ownership Summary

```text
OpenSpec                 Kafa local runtime              Native host
----------------------   -----------------------------   ----------------------
proposal/design/specs    acceptance links               task/thread/subagent
tasks and archive        root-owned task status          worktree and approval
behavioral authority     immutable verification          model/cancel/handoff
                         findings/gate/delivery
```

This separation is the product contract: one specification authority, one
native lifecycle owner, and one local verified-delivery fact source.
