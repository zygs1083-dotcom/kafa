---
name: "project-bootstrap"
description: "Use before requirement baselining for new or substantial code-delivery work to inspect and initialize the project workspace, git state, branch strategy, harness control files, and optional collaboration tools such as GitHub, Linear, Notion, Figma, and Slack. Produces a capability report and tool mapping before implementation. Codex should decide which tools are useful from project context and only ask before high-impact external actions."
---

# Project Bootstrap

Prepare the workspace and collaboration control plane before requirements are frozen.

## Boundary

Bootstrap owns local setup, capability discovery, tool mapping, and safe recommendations. It does not deploy, release to production, provision paid resources, change secrets, or perform high-impact external actions without explicit user confirmation.

## Checks

1. Inspect whether the workspace is inside a git repository.
2. If there is no git repository, initialize git when it is clearly useful and low risk; ask only when the workspace has unrelated files or the project context is ambiguous.
3. Inspect branch, remote, and dirty worktree state when git exists.
4. Recommend a feature branch for non-trivial work.
5. Initialize or repair `.ai-team/` and `docs/harness/` with `scripts/init_project_harness.py` when appropriate.
6. Use `project-runtime` to update phase to `project_bootstrap` and record bootstrap decisions.
7. Detect whether project instructions such as `AGENTS.md` exist.
8. Identify available or requested collaboration systems:
   - GitHub for repository, branches, issues, PRs, checks, and code review.
   - Linear for project, issue, milestone, and implementation status.
   - Notion for PRD, decision log, architecture notes, and delivery records.
   - Figma for design context, prototypes, components, and visual acceptance.
   - Slack for status updates, review requests, and delivery notifications.
9. Decide the source of truth for each artifact. Default to local harness files when an external tool is unavailable or not useful for the current project.

## Autonomy Policy

Codex should decide whether GitHub, Linear, Notion, Figma, or Slack is useful. Do not ask the user to choose tools unless the project context is genuinely ambiguous.

Proceed without extra confirmation for low-risk project-management writes when the tool is clearly useful and the target is clear:

- local git initialization or feature branch creation,
- local harness file initialization,
- draft Notion/project documentation,
- Linear or GitHub issue/task creation for the confirmed scope,
- draft GitHub PR creation after code is ready,
- updating local task-board or delivery records.

Ask before high-impact external actions:

- sending Slack messages to people or channels,
- creating public repositories, public pages, or public design files,
- changing secrets, credentials, billing, permissions, or production configuration,
- creating paid resources,
- destructive changes or irreversible migrations,
- broad edits to existing shared Notion/Figma/GitHub/Linear artifacts.

When confirmation is required, use:

```text
I plan to write to:
- Tool:
- Target:
- Change:
- Reason:

Please confirm before I proceed.
```

Read-only inspection can proceed when the relevant connector is available and the user request implies that tool context matters.

## Runtime Updates

After bootstrap, update local control files:

```bash
python3 plugins/codex-project-harness/scripts/update_phase.py project_bootstrap --status active --owner bootstrap-coordinator
python3 plugins/codex-project-harness/scripts/record_decision.py --decision "Selected project tooling" --reason "Workspace and collaboration context inspected"
```

## Tool Mapping

Use this default mapping unless the user chooses otherwise:

| Artifact | Default | Optional external system |
| --- | --- | --- |
| Requirement baseline | `.ai-team/requirements/requirements.md` | Notion PRD |
| Acceptance criteria | `.ai-team/requirements/acceptance.md` | Linear/GitHub issue checklist, Notion |
| Task board | `.ai-team/planning/task-board.md` | Linear issues, GitHub issues |
| Design context | `docs/harness/design-context.md` | Figma |
| Implementation evidence | git diff, local tests | GitHub branch/PR/checks |
| QA findings | `docs/harness/validation.md` | GitHub review, Linear comments, Notion QA notes |
| Delivery handoff | `docs/harness/delivery.md` | GitHub PR summary, Notion delivery note, Slack update |

## Output

```text
# Project Bootstrap

## Workspace
## Git
## Harness Files
## Collaboration Tools
## Source Of Truth
## Recommended Setup
## High-Impact Actions Requiring Confirmation
## Next Step
```

## Rule

Do not let missing external tools block local code delivery. Fall back to `.ai-team/` and `docs/harness/`, record the limitation, and continue.
