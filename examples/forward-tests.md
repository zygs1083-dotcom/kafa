# Forward Test Prompts

Use these prompts to validate whether the harness skills produce the intended behavior in fresh Codex sessions.

Executable runtime forward evals live in:

```bash
python3 plugins/codex-project-harness/scripts/run_forward_eval.py
```

The runner writes regression results to:

```text
docs/runtime/forward-eval-results.json
```

The prompts below are still useful for fresh-session qualitative evaluation.

## Full Project

```text
Use $project-harness from this repository to build a small task tracker app. Stop at verified code delivery. Decide whether GitHub, Linear, Notion, Figma, or Slack is useful from context.
```

Expected behavior:

- Runs bootstrap before requirement baseline.
- Initializes local harness files when needed.
- Confirms or states the requirement baseline.
- Creates task board rows mapped to acceptance criteria.
- Records validation evidence before delivery readiness.
- Does not deploy.

## Clear Feature

```text
Use $project-harness from this repository to add CSV export to the current app.
```

Expected behavior:

- Uses lightweight bootstrap if git/tooling context matters.
- Creates or updates focused tasks.
- Uses test-first delivery when a stable export contract exists.
- Records validation and delivery evidence.

## Bug Fix

```text
Use $project-harness from this repository to fix the failing login redirect and prevent regression.
```

Expected behavior:

- Routes to `bug-fix-loop`.
- Reproduces or characterizes the failure.
- Records task and validation evidence.
- Runs QA before delivery readiness.

## Tool-Heavy Project

```text
Use $project-harness from this repository. Build a settings page from an existing Figma design and track tasks in Linear. Use GitHub PR evidence for QA.
```

Expected behavior:

- Uses project-bootstrap.
- Maps design context to Figma.
- Maps tasks to Linear.
- Uses GitHub PR/check evidence when available.
- Records all mappings in local harness files.
