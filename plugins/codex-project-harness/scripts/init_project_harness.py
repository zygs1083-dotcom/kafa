#!/usr/bin/env python3
"""Create the project control-plane files used by Codex Project Harness."""

from __future__ import annotations

from pathlib import Path


FILES = {
    ".ai-team/control/capability-report.md": "# Capability Report\n\n- Status: pending\n",
    ".ai-team/control/project-charter.md": "# Project Charter\n\n- Status: draft\n",
    ".ai-team/control/project-state.yaml": "status: draft\nphase: discovery\n",
    ".ai-team/control/agent-registry.md": "# Agent Registry\n\n| Agent | Role | Status |\n| --- | --- | --- |\n",
    ".ai-team/control/risk-register.md": "# Risk Register\n\n| Risk | Impact | Mitigation |\n| --- | --- | --- |\n",
    ".ai-team/control/decision-log.md": "# Decision Log\n\n| Date | Decision | Reason |\n| --- | --- | --- |\n",
    ".ai-team/requirements/requirements.md": "# Requirements\n\n",
    ".ai-team/requirements/acceptance.md": "# Acceptance Criteria\n\n",
    ".ai-team/requirements/traceability.md": "# Traceability\n\n| Requirement | Implementation | Test |\n| --- | --- | --- |\n",
    ".ai-team/planning/roadmap.md": "# Roadmap\n\n",
    ".ai-team/planning/task-board.md": "# Task Board\n\n| Task | Owner | Status |\n| --- | --- | --- |\n",
    "docs/harness/team-architecture.md": "# Team Architecture\n\n",
    "docs/harness/workflow.md": "# Workflow\n\n",
    "docs/harness/validation.md": "# Validation\n\n",
    "docs/harness/evolution-log.md": "# Evolution Log\n\n",
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
