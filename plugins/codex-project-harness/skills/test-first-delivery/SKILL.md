---
name: "test-first-delivery"
description: "Use when the user asks to implement with tests first, add regression coverage, validate contracts, or deliver a feature through test-backed development. Trigger for 先写测试, 测试驱动, 补回归测试, 验证契约, test first, TDD, regression coverage, contract validation."
---

# Test-First Delivery

Prefer evidence before implementation confidence.

## Workflow

1. Map the requirement to acceptance criteria.
2. Identify the contract: API shape, data schema, UI behavior, command output, or integration boundary.
3. Link the contract to acceptance IDs and failure mode IDs when present.
4. Link the contract to local or OpenSpec requirement and acceptance references when present.
5. Add a failing test or executable check when practical.
6. Implement the smallest code needed to pass.
7. Add edge-case and regression checks proportional to risk.
8. Run relevant tests and inspect failures.
9. Let the root controller register the exact command with `test-target add`, link it to the task, qualify the target for the acceptance with an explicit rationale, and run it with `verify run` on the current candidate.
10. Ensure the final test proves behavior, not just existence.

`verify run` is the only supported path from command execution to immutable,
gate-eligible execution and validation facts. Workers return commands and
results through the Native Codex/ChatGPT host; they never mutate Kafa state.
Free-form `validation record` is judgment-only and cannot substitute for an
execution. Skipped, blocked, not-run, and fixture-only checks are not passes.
Gate-eligible schema 31 execution requires complete target/controller provenance,
including `target_definition_sha256`, `runtime_executable_sha256`,
`policy_version`, and `provenance_status=complete`; `legacy-incomplete` history
cannot cover a current acceptance. Medium/high/critical unit or integration
failure-mode coverage must use a supported structured result, while regex remains
available only for documented low-risk paths. Container evidence additionally
requires a frozen local `container_engine_endpoint`; remote/ambiguous routing and
truncated Go/nextest streams fail closed.

## Test Exception Rule

If no automated or executable test is practical, record a reason code and alternate verification:

```text
Reason code: docs-only | exploratory | unavailable-dependency | legacy-no-test-hook | time-boxed-risk-accepted
Alternate verification:
Risk owner:
```

## Completion Evidence

Report:

- test added or updated,
- command run,
- result,
- behavior covered,
- immutable execution and validation IDs,
- local artifact paths and record IDs,
- failure modes covered or exemption reason,
- known gaps.
