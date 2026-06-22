# Collaboration Tool Integration

Use this reference when `project-harness` or `project-bootstrap` needs to coordinate GitHub, Linear, Notion, Figma, or Slack during code delivery.

## Integration Principles

- Prefer local harness files as the fallback source of truth.
- Let Codex decide which external tools are useful from project context, existing links, available connectors, and project scale.
- Read external context before writing to it.
- Ask for explicit confirmation only before high-impact external actions; proceed with low-risk project-management writes when the target and purpose are clear.
- Keep mappings bidirectional: local task IDs should reference external issue/page/PR/design IDs, and external records should reference the local delivery scope when practical.
- Deployment and production release remain out of scope.

## Stage Mapping

| Stage | Git / GitHub | Linear | Notion | Figma | Slack |
| --- | --- | --- | --- | --- | --- |
| Bootstrap | Inspect repo, branch, remote, auth, open PR context | Detect existing project/issues when useful | Detect PRD/decision docs when useful | Detect design link/context when useful | Detect channel/thread when useful |
| Requirements | Link scope to issue/PRD | Create or map epic/issues when useful | Draft or update PRD when useful | Pull design constraints or infer need for design source | Share clarification summary only when useful |
| Planning | Create feature branch; map task IDs to issue IDs | Create/update issues when useful | Record plan/decisions when useful | Create design tasks or attach frames when useful | Share plan only when useful |
| Implementation | Commit locally; push/open draft PR when useful | Move issue status when useful | Record implementation notes when useful | Compare UI to design when relevant | Post progress updates only when useful |
| QA | Use PR checks/reviews when available | Record QA status when useful | Record QA notes when useful | Validate visual acceptance against Figma | Request review/status update only when useful |
| Delivery | Summarize diff/branch/PR/checks | Mark delivery-ready when useful | Publish delivery note when useful | Link final design/implementation status | Send handoff only when useful |

## Safe Defaults

If the user does not choose a tool stack, Codex decides from context:

```text
Git: use local git if present; recommend initialization if absent.
GitHub: use only if a remote or explicit repo exists.
Linear: do not create issues by default.
Notion: do not create pages by default.
Figma: use only if a design link/context is provided or requested.
Slack: do not send messages by default.
```

For larger projects, Codex may enable Linear, Notion, GitHub issues/PRs, and Figma without asking when the workspace context makes the target clear. Slack remains high-visibility; ask before sending messages.

## Capability Report Shape

```text
Tool:
Status: available | unavailable | not requested | needs confirmation
Purpose:
Source of truth:
External writes:
Fallback:
Notes:
```
