#!/usr/bin/env python3
"""Create the project control-plane files used by Codex Project Harness."""

from __future__ import annotations

from pathlib import Path


FILES = {
    ".ai-team/control/capability-report.md": "# Capability Report\n\n| Capability | Status | Evidence | Fallback |\n| --- | --- | --- | --- |\n| Git | pending | | local files |\n| GitHub | pending | | git diff |\n| Linear | pending | | `.ai-team/planning/task-board.md` |\n| Notion | pending | | `docs/harness/` |\n| Figma | pending | | `docs/harness/design-context.md` |\n| Slack | pending | | final response |\n",
    ".ai-team/control/project-charter.md": "# Project Charter\n\n- Status: draft\n",
    ".ai-team/control/project-state.yaml": "status: draft\nphase: intake\nscope_status: unconfirmed\ncurrent_owner: project-manager\nblocked_reason: null\n",
    ".ai-team/control/agent-registry.md": "# Agent Registry\n\n| Agent | Role | Status |\n| --- | --- | --- |\n",
    ".ai-team/control/tooling-map.md": "# Tooling Map\n\n| Artifact | Source Of Truth | External Tool | External ID / Link | Fallback |\n| --- | --- | --- | --- | --- |\n| Requirements | local | | | `.ai-team/requirements/requirements.md` |\n| Acceptance | local | | | `.ai-team/requirements/acceptance.md` |\n| Tasks | local | | | `.ai-team/planning/task-board.md` |\n| Design | local | | | `docs/harness/design-context.md` |\n| Validation | local | | | `docs/harness/validation.md` |\n| Delivery | local | | | `docs/harness/delivery.md` |\n",
    ".ai-team/control/risk-register.md": "# Risk Register\n\n| Risk | Impact | Mitigation |\n| --- | --- | --- |\n",
    ".ai-team/control/decision-log.md": "# Decision Log\n\n| Date | Decision | Reason |\n| --- | --- | --- |\n",
    ".ai-team/requirements/requirements.md": "# Requirements\n\n## Goal\n\n## Users\n\n## Scenarios\n\n## Functional Requirements\n\n## Non-Functional Requirements\n\n## Non-Goals\n\n## Tool Mapping\n\n",
    ".ai-team/requirements/acceptance.md": "# Acceptance Criteria\n\n| ID | Criterion | Priority | Tool Link | Status |\n| --- | --- | --- | --- | --- |\n",
    ".ai-team/requirements/traceability.md": "# Traceability\n\n| Requirement | Acceptance | Task | Implementation | Test | External Link |\n| --- | --- | --- | --- | --- | --- |\n",
    ".ai-team/planning/roadmap.md": "# Roadmap\n\n",
    ".ai-team/planning/task-board.md": "# Task Board\n\n| ID | Task | Owner | Status | Acceptance | Depends On | Tool Link | Evidence |\n| --- | --- | --- | --- | --- | --- | --- | --- |\n",
    "docs/harness/bootstrap.md": "# Bootstrap\n\n## Workspace\n\n## Git\n\n## Harness Files\n\n## Collaboration Tools\n\n## Source Of Truth\n\n## Recommended Setup\n\n## Next Step\n\n",
    "docs/harness/team-architecture.md": "# Team Architecture\n\n",
    "docs/harness/workflow.md": "# Workflow\n\n",
    "docs/harness/design-context.md": "# Design Context\n\n| Source | Link / ID | Relevant Screens | Acceptance Notes |\n| --- | --- | --- | --- |\n",
    "docs/harness/validation.md": "# Validation\n\n| Surface | Acceptance | Tool Context | Commands | Findings | Pass/Fail | Residual Risk |\n| --- | --- | --- | --- | --- | --- | --- |\n",
    "docs/harness/delivery.md": "# Delivery\n\n## Scope\n\n## Acceptance Mapping\n\n## Changed Files\n\n## Validation\n\n## Independent QA\n\n## Collaboration Links\n\n## Data / Config Notes\n\n## Known Gaps\n\n## Handoff Notes\n\n## Out Of Scope\n\n",
    "docs/harness/evolution-log.md": "# Evolution Log\n\n| Date | Change | Evidence | Tooling Notes |\n| --- | --- | --- | --- |\n",
    ".codex/agents/.gitkeep": "",
    ".agents/skills/.gitkeep": "",
}


def main() -> int:
    root = Path.cwd()
    for relpath, content in FILES.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(content, encoding="utf-8")

    gitignore = root / ".gitignore"
    line = ".ai-team/runtime/\n"
    if gitignore.exists():
        text = gitignore.read_text(encoding="utf-8")
        if line.strip() not in text.splitlines():
            gitignore.write_text(text.rstrip() + "\n" + line, encoding="utf-8")
    else:
        gitignore.write_text(line, encoding="utf-8")

    print("OK: project harness initialized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
