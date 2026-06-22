# Tool Adapters

Use this reference when `project-runtime`, `project-bootstrap`, or `project-harness` needs to sync local harness state with external tools.

## General Contract

Each adapter decision should produce:

```text
Tool:
Action:
Local artifact:
External target:
External ID / link:
Evidence:
Fallback:
Confirmation needed: yes | no
```

Local harness files remain the minimum source of truth. External tools enrich the workflow, but they must not be required for local code delivery.

## Git / GitHub

Use Git/GitHub when there is a git repository, a GitHub remote, a linked PR, or useful issue/check context.

Typical actions:

- create or switch to a feature branch for non-trivial work,
- map task IDs to GitHub issues,
- open a draft PR when code is ready for review,
- use PR checks and review comments as QA evidence,
- summarize changed files and commit IDs in delivery.

Ask before:

- creating a public repository,
- force-pushing,
- deleting branches,
- changing repository settings,
- merging to protected branches.

## Linear

Use Linear when a project, issue, or team workflow is available or clearly useful for multi-task delivery.

Typical actions:

- create project or issue records for confirmed scope,
- map acceptance criteria to issue descriptions or checklists,
- update issue status as tasks move through implementation and QA,
- record blocker or residual-risk links in delivery.

Ask before:

- bulk editing many issues,
- changing team/project configuration,
- closing or deleting issues outside the current delivery scope.

## Notion

Use Notion when PRD, decision, architecture, QA, or delivery notes should live in a shared knowledge base.

Typical actions:

- create or update a PRD from the requirement baseline,
- record architecture decisions,
- record QA summaries and delivery handoff,
- link Notion pages from `.ai-team/control/tooling-map.md`.

Ask before:

- editing large shared pages,
- moving pages between databases,
- changing permissions,
- publishing pages publicly.

## Figma

Use Figma when design context, prototypes, components, or visual acceptance matter.

Typical actions:

- read design context and frame references,
- map visual acceptance criteria to Figma frames,
- validate UI implementation against provided design references,
- record final design/implementation status in delivery.

Ask before:

- creating or editing shared Figma files,
- changing design system components,
- publishing design files.

## Slack

Use Slack for team-facing communication only when a project channel, thread, or stakeholder context is clear.

Typical actions:

- prepare clarification summaries,
- request review,
- send delivery handoff,
- share blocker status.

Always ask before sending Slack messages to people or channels.
