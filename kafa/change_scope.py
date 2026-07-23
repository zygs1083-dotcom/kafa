"""Closed, conservative release change-scope classification.

The classifier only reduces real-Native evidence pressure for paths it can
identify exactly.  Unknown input selects the blocking single and parallel
profiles; deterministic gates are never optional.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Literal, Mapping


CHANGE_SCOPE_VERSION = "kafa-change-scope-v1"
BLOCKING_NATIVE_PROFILES = ("live-codex", "live-codex-parallel")

Scope = Literal[
    "host",
    "packaging",
    "release-tooling",
    "native-evaluator",
    "schema-runtime",
    "docs-only",
    "unknown",
]

SCOPE_ORDER: tuple[Scope, ...] = (
    "host",
    "packaging",
    "release-tooling",
    "native-evaluator",
    "schema-runtime",
    "docs-only",
    "unknown",
)
BLOCKING_SCOPES: frozenset[Scope] = frozenset(
    {"host", "packaging", "release-tooling", "native-evaluator", "unknown"}
)
FULL_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
NO_RELEASE_BASE_OID = "0" * 40
NO_RELEASE_BASE_ISSUE = "no published release base is available"

_HOST_EXACT = frozenset(
    {
        "kafa/cli.py",
        "kafa/codex_app_server.py",
        "tests/test_codex_hooks.py",
        "tests/test_native_agents_install.py",
        "tests/test_native_host_ownership.py",
        "tests/test_project_entrypoint.py",
    }
)
_HOST_PREFIXES = (
    "plugins/codex-project-harness/hooks/",
    "plugins/codex-project-harness/templates/agents/",
)
_PACKAGING_EXACT = frozenset(
    {
        "LICENSE",
        "MANIFEST.in",
        "VERSION",
        "marketplace.json",
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "uv.lock",
        "poetry.lock",
        "release.json",
        "kafa/__init__.py",
        "kafa/version.py",
        "plugins/codex-project-harness/references/distribution-manifest.json",
        "tests/run_isolated_install_smoke.py",
        "tests/test_distribution_manifest.py",
        "tests/test_install_release.py",
    }
)
_PACKAGING_PREFIXES = (
    "plugins/codex-project-harness/.codex-plugin/",
    "plugins/codex-project-harness/.claude-plugin/",
    "requirements/",
)
_RELEASE_EXACT = frozenset(
    {
        "CHANGELOG.md",
        "release-tooling.json",
        "kafa/release.py",
        "kafa/rehearsal.py",
        "kafa/supply_chain.py",
        "kafa/artifact_subject.py",
        "kafa/change_scope.py",
        "kafa/evidence_summary.py",
        "tests/test_release_contract.py",
        "tests/test_release_rehearsal.py",
        "tests/test_change_scope.py",
        "tests/test_supply_chain.py",
    }
)
_RELEASE_PREFIXES = (".github/",)
_NATIVE_EVALUATOR_EXACT = frozenset(
    {
        "plugins/codex-project-harness/scripts/run_agent_e2e_eval.py",
        "tests/test_agent_e2e_eval.py",
    }
)
_NATIVE_EVALUATOR_PREFIXES = (
    "docs/runtime/native-codex-",
    "docs/runtime/fresh-skill-eval-",
)
_SCHEMA_RUNTIME_PREFIXES = (
    "benchmarks/",
    "plugins/codex-project-harness/core/",
    "plugins/codex-project-harness/docs/",
    "plugins/codex-project-harness/references/",
    "plugins/codex-project-harness/schemas/",
    "plugins/codex-project-harness/scripts/",
    "plugins/codex-project-harness/skills/",
    "plugins/codex-project-harness/templates/project/",
    "tests/",
    "tools/",
)
_DOCS_EXACT = frozenset({"AGENTS.md", "INSTALL.md", "QUICKSTART.md", "README.md"})
_DOCS_PREFIXES = ("docs/", "examples/", "openspec/")


@dataclass(frozen=True)
class PathScope:
    path: str
    scope: Scope

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "scope": self.scope}


@dataclass(frozen=True)
class ChangeScopeDecision:
    state: Literal["classified", "unknown"]
    base_oid: str
    head_oid: str
    changed_paths: tuple[str, ...]
    path_scopes: tuple[PathScope, ...]
    scopes: tuple[Scope, ...]
    unknown_paths: tuple[str, ...]
    issues: tuple[str, ...]
    native_requirement: Literal["blocking", "advisory"]
    required_profiles: tuple[str, ...]
    deterministic_gates_required: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "version": CHANGE_SCOPE_VERSION,
            "state": self.state,
            "base_oid": self.base_oid,
            "head_oid": self.head_oid,
            "changed_paths": list(self.changed_paths),
            "changed_paths_sha256": changed_paths_sha256(self.changed_paths),
            "path_scopes": [item.to_dict() for item in self.path_scopes],
            "scopes": list(self.scopes),
            "unknown_paths": list(self.unknown_paths),
            "issues": list(self.issues),
            "native_requirement": self.native_requirement,
            "required_profiles": list(self.required_profiles),
            "deterministic_gates_required": self.deterministic_gates_required,
        }


def changed_paths_sha256(paths: Iterable[str]) -> str:
    """Hash an ordered path set without delimiter ambiguity."""

    digest = hashlib.sha256()
    for path in tuple(paths):
        encoded = path.encode("utf-8", errors="strict")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def validate_decision_report(
    report: Mapping[str, Any],
    *,
    expected_base_oid: str | None = None,
    expected_head_oid: str | None = None,
    expected_changed_paths: Iterable[str] | None = None,
) -> list[str]:
    """Validate every field of a serialized change-scope decision.

    Optional expected values bind the classifier output to an independently
    resolved Git comparison instead of trusting caller-provided report fields.
    """

    expected_keys = {
        "version",
        "state",
        "base_oid",
        "head_oid",
        "changed_paths",
        "changed_paths_sha256",
        "path_scopes",
        "scopes",
        "unknown_paths",
        "issues",
        "native_requirement",
        "required_profiles",
        "deterministic_gates_required",
    }
    if not isinstance(report, Mapping) or set(report) != expected_keys:
        actual = sorted(report) if isinstance(report, Mapping) else type(report).__name__
        return [f"change-scope report keys mismatch: actual={actual} expected={sorted(expected_keys)}"]

    errors: list[str] = []
    if report.get("version") != CHANGE_SCOPE_VERSION:
        errors.append("change-scope report version is unsupported")
    state = report.get("state")
    if state not in {"classified", "unknown"}:
        errors.append("change-scope report state is invalid")
    base_oid = report.get("base_oid")
    head_oid = report.get("head_oid")
    if (
        not isinstance(base_oid, str)
        or not FULL_OID.fullmatch(base_oid)
        or (
            base_oid == "0" * len(base_oid)
            and base_oid != NO_RELEASE_BASE_OID
        )
    ):
        errors.append("change-scope report base_oid is invalid")
    if (
        not isinstance(head_oid, str)
        or not FULL_OID.fullmatch(head_oid)
        or head_oid == "0" * len(head_oid)
    ):
        errors.append("change-scope report head_oid is invalid")
    no_release_base = base_oid == NO_RELEASE_BASE_OID
    if no_release_base and state != "unknown":
        errors.append("no-release base must remain an unknown change-scope decision")
    if expected_base_oid is not None and base_oid != expected_base_oid:
        errors.append("change-scope report base_oid does not match the independent comparison")
    if expected_head_oid is not None and head_oid != expected_head_oid:
        errors.append("change-scope report head_oid does not match the independent comparison")

    raw_paths = report.get("changed_paths")
    paths: tuple[str, ...] = ()
    if (
        not isinstance(raw_paths, list)
        or any(not isinstance(path, str) for path in raw_paths)
        or raw_paths != sorted(set(raw_paths))
    ):
        errors.append("change-scope report changed_paths are invalid")
    else:
        paths = tuple(raw_paths)
    if expected_changed_paths is not None:
        try:
            expected_paths = tuple(sorted(set(expected_changed_paths)))
        except TypeError:
            expected_paths = ()
            errors.append("independent changed path set is invalid")
        if paths != expected_paths:
            errors.append("change-scope report paths do not match the independent Git diff")
    try:
        actual_path_digest = changed_paths_sha256(paths)
    except UnicodeError:
        actual_path_digest = ""
        errors.append("change-scope report paths are not UTF-8 encodable")
    if report.get("changed_paths_sha256") != actual_path_digest:
        errors.append("change-scope report path-set digest is invalid")

    if report.get("deterministic_gates_required") is not True:
        errors.append("change-scope report attempted to disable deterministic gates")

    if state == "classified" and isinstance(base_oid, str) and isinstance(head_oid, str):
        if not paths:
            errors.append("classified change-scope report has no paths")
        else:
            expected = classify_changed_paths(paths, base_oid=base_oid, head_oid=head_oid).to_dict()
            for field in (
                "state",
                "path_scopes",
                "scopes",
                "unknown_paths",
                "issues",
                "native_requirement",
                "required_profiles",
                "deterministic_gates_required",
            ):
                if report.get(field) != expected.get(field):
                    errors.append(f"change-scope report {field} is inconsistent with its paths")
    elif state == "unknown":
        unknown_contract = {
            "path_scopes": [],
            "scopes": ["unknown"],
            "unknown_paths": [],
            "native_requirement": "blocking",
            "required_profiles": list(BLOCKING_NATIVE_PROFILES),
        }
        if paths:
            errors.append("unknown change-scope report must not claim a path inventory")
        for field, expected_value in unknown_contract.items():
            if report.get(field) != expected_value:
                errors.append(f"unknown change-scope report {field} is invalid")
        issues = report.get("issues")
        if not isinstance(issues, list) or not issues or any(
            not isinstance(issue, str) or not issue.strip() for issue in issues
        ):
            errors.append("unknown change-scope report requires actionable issues")
        elif no_release_base and issues != [NO_RELEASE_BASE_ISSUE]:
            errors.append("no-release change-scope report issue is invalid")
    return errors


def select_published_release_base(
    repo: Path,
    *,
    head_oid: str,
    releases: Iterable[Mapping[str, Any]],
) -> str | None:
    """Select the newest published, non-draft release reachable from head.

    Arbitrary local tags are never candidates. Missing, malformed, deleted, or
    unrelated published tags are ignored so the caller can fail closed.
    """

    if not FULL_OID.fullmatch(head_oid):
        return None
    repo = repo.expanduser().resolve()
    if not repo.is_dir():
        return None
    try:
        resolved_head = _git(repo, "rev-parse", "--verify", f"{head_oid}^{{commit}}").decode(
            "ascii"
        ).strip()
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None
    if resolved_head != head_oid:
        return None

    candidates: list[tuple[datetime, str, str]] = []
    for release in releases:
        if not isinstance(release, Mapping) or release.get("draft") is not False:
            continue
        tag_name = release.get("tag_name")
        published_at = release.get("published_at")
        if not isinstance(tag_name, str) or not tag_name or not isinstance(published_at, str):
            continue
        try:
            published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            if published.tzinfo is None:
                continue
            base_oid = _git(
                repo, "rev-parse", "--verify", f"refs/tags/{tag_name}^{{commit}}"
            ).decode("ascii").strip()
            if not FULL_OID.fullmatch(base_oid) or base_oid == head_oid:
                continue
            _git(repo, "merge-base", "--is-ancestor", base_oid, head_oid)
        except (OSError, UnicodeError, ValueError, subprocess.SubprocessError):
            continue
        candidates.append((published, tag_name, base_oid))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return candidates[0][2]


def _valid_repo_path(path: str) -> bool:
    if not path or "\\" in path or "\x00" in path:
        return False
    pure = PurePosixPath(path)
    return not pure.is_absolute() and all(part not in {"", ".", ".."} for part in pure.parts)


def classify_path(path: str) -> Scope:
    if not _valid_repo_path(path):
        return "unknown"
    if path in _HOST_EXACT or path.startswith(_HOST_PREFIXES):
        return "host"
    if path in _PACKAGING_EXACT or path.startswith(_PACKAGING_PREFIXES):
        return "packaging"
    if path in _RELEASE_EXACT or path.startswith(_RELEASE_PREFIXES):
        return "release-tooling"
    if path in _NATIVE_EVALUATOR_EXACT or path.startswith(_NATIVE_EVALUATOR_PREFIXES):
        return "native-evaluator"
    if path.startswith(_SCHEMA_RUNTIME_PREFIXES):
        return "schema-runtime"
    if path in _DOCS_EXACT or path.startswith(_DOCS_PREFIXES):
        return "docs-only"
    return "unknown"


def classify_changed_paths(
    paths: Iterable[str],
    *,
    base_oid: str,
    head_oid: str,
) -> ChangeScopeDecision:
    if (
        not FULL_OID.fullmatch(base_oid)
        or not FULL_OID.fullmatch(head_oid)
        or head_oid == "0" * len(head_oid)
        or (
            base_oid == "0" * len(base_oid)
            and base_oid != NO_RELEASE_BASE_OID
        )
    ):
        return _unknown_decision(base_oid, head_oid, "base and head must be exact full object ids")
    if base_oid == NO_RELEASE_BASE_OID:
        return _unknown_decision(base_oid, head_oid, NO_RELEASE_BASE_ISSUE)
    changed_paths = tuple(sorted(set(paths)))
    if not changed_paths:
        return _unknown_decision(base_oid, head_oid, "changed path set is empty or unavailable")
    path_scopes = tuple(PathScope(path, classify_path(path)) for path in changed_paths)
    present = {item.scope for item in path_scopes}
    scopes = tuple(scope for scope in SCOPE_ORDER if scope in present)
    unknown_paths = tuple(item.path for item in path_scopes if item.scope == "unknown")
    blocking = bool(present & BLOCKING_SCOPES)
    return ChangeScopeDecision(
        state="classified",
        base_oid=base_oid,
        head_oid=head_oid,
        changed_paths=changed_paths,
        path_scopes=path_scopes,
        scopes=scopes,
        unknown_paths=unknown_paths,
        issues=(),
        native_requirement="blocking" if blocking else "advisory",
        required_profiles=BLOCKING_NATIVE_PROFILES if blocking else (),
    )


def classify_repository(repo: Path, *, base_oid: str, head_oid: str) -> ChangeScopeDecision:
    if (
        not FULL_OID.fullmatch(base_oid)
        or not FULL_OID.fullmatch(head_oid)
        or head_oid == "0" * len(head_oid)
        or (
            base_oid == "0" * len(base_oid)
            and base_oid != NO_RELEASE_BASE_OID
        )
    ):
        return _unknown_decision(base_oid, head_oid, "base and head must be exact full object ids")
    if base_oid == NO_RELEASE_BASE_OID:
        return _unknown_decision(base_oid, head_oid, NO_RELEASE_BASE_ISSUE)
    repo = repo.expanduser().resolve()
    if not repo.is_dir():
        return _unknown_decision(base_oid, head_oid, f"repository directory is unavailable: {repo}")
    try:
        resolved_base = _git(repo, "rev-parse", "--verify", f"{base_oid}^{{commit}}").decode("ascii").strip()
        resolved_head = _git(repo, "rev-parse", "--verify", f"{head_oid}^{{commit}}").decode("ascii").strip()
        if resolved_base != base_oid or resolved_head != head_oid:
            raise ValueError("object ids did not resolve exactly")
        _git(repo, "merge-base", "--is-ancestor", base_oid, head_oid)
        raw_paths = _git(
            repo,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            "--diff-filter=ACDMRTUXB",
            base_oid,
            head_oid,
            "--",
        )
        resolved_base_after = _git(repo, "rev-parse", "--verify", f"{base_oid}^{{commit}}").decode("ascii").strip()
        resolved_head_after = _git(repo, "rev-parse", "--verify", f"{head_oid}^{{commit}}").decode("ascii").strip()
        if (resolved_base_after, resolved_head_after) != (base_oid, head_oid):
            raise ValueError("object identity changed while deriving the diff")
        parts = raw_paths.split(b"\0")
        if not parts or parts[-1] != b"":
            raise ValueError("Git path output is not NUL terminated")
        decoded = [part.decode("utf-8", errors="strict") for part in parts[:-1]]
        if len(decoded) != len(set(decoded)):
            raise ValueError("Git path output contains duplicates")
        return classify_changed_paths(decoded, base_oid=base_oid, head_oid=head_oid)
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        return _unknown_decision(base_oid, head_oid, f"change diff is unavailable: {exc}")


def _unknown_decision(base_oid: str, head_oid: str, issue: str) -> ChangeScopeDecision:
    return ChangeScopeDecision(
        state="unknown",
        base_oid=base_oid,
        head_oid=head_oid,
        changed_paths=(),
        path_scopes=(),
        scopes=("unknown",),
        unknown_paths=(),
        issues=(issue,),
        native_requirement="blocking",
        required_profiles=BLOCKING_NATIVE_PROFILES,
    )


def _git(repo: Path, *arguments: str) -> bytes:
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("GIT_") and name not in {"GIT_SSH", "GIT_SSH_COMMAND"}:
            env.pop(name, None)
    completed = subprocess.run(
        ["git", "-c", "core.quotepath=false", *arguments],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise subprocess.CalledProcessError(completed.returncode, completed.args, completed.stdout, detail)
    return completed.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify release changes for Native evidence selection.")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base-oid", required=True)
    parser.add_argument("--head-oid", required=True)
    args = parser.parse_args(argv)
    decision = classify_repository(
        Path(args.repo),
        base_oid=args.base_oid,
        head_oid=args.head_oid,
    )
    print(json.dumps(decision.to_dict(), indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
