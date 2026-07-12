"""Static release contract validation for Kafa source and tag workflows."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any


RELEASE_MANIFEST = "release.json"
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+-beta\.\d+$")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Kafa release source, tag, and artifact metadata.")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--require-tag", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = release_report(Path(args.repo).expanduser().resolve(), require_tag=args.require_tag)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for check in report["checks"]:
            print(f"{'OK' if check['ok'] else 'ERROR'}: {check['name']}: {check['details']}")
    return 0 if report["ok"] else 1


def release_report(repo: Path, *, require_tag: bool = False) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    manifest_path = repo / RELEASE_MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("release manifest root must be an object")
        add_check(checks, "release manifest", True, str(manifest_path))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        add_check(checks, "release manifest", False, str(exc))
        return {"ok": False, "repo": str(repo), "checks": checks}

    version = str(manifest.get("version", ""))
    pep440 = str(manifest.get("pep440_version", ""))
    tag = str(manifest.get("tag", ""))
    state = str(manifest.get("release_state", ""))
    expected_pep440 = version.replace("-beta.", "b") if "-beta." in version else version
    add_check(checks, "version syntax", bool(VERSION_PATTERN.fullmatch(version)), version)
    add_check(checks, "PEP 440 version", pep440 == expected_pep440, f"manifest={pep440} expected={expected_pep440}")
    add_check(checks, "release tag", tag == f"v{version}", f"manifest={tag} expected=v{version}")
    add_check(checks, "release state", state in {"development", "release"}, state)
    add_check(checks, "release channel", manifest.get("channel") == "prerelease", str(manifest.get("channel", "")))
    codex_version = str(manifest.get("codex_cli_smoke_version", ""))
    add_check(checks, "Codex CLI smoke version", bool(re.fullmatch(r"\d+\.\d+\.\d+", codex_version)), codex_version)
    add_check(checks, "package identity", manifest.get("package") == "kafa", str(manifest.get("package", "")))
    add_check(checks, "plugin identity", manifest.get("plugin") == "codex-project-harness", str(manifest.get("plugin", "")))

    root_version = read_text(repo / "VERSION").strip()
    add_check(checks, "VERSION alignment", root_version == version, f"VERSION={root_version} manifest={version}")
    plugin = read_json(repo / "plugins" / "codex-project-harness" / ".codex-plugin" / "plugin.json")
    add_check(checks, "plugin version alignment", plugin.get("version") == version, f"plugin={plugin.get('version', '')} manifest={version}")
    package = read_toml(repo / "pyproject.toml").get("project", {})
    package = package if isinstance(package, dict) else {}
    add_check(checks, "package version alignment", package.get("version") == pep440, f"package={package.get('version', '')} manifest={pep440}")
    add_check(checks, "package name alignment", package.get("name") == manifest.get("package"), str(package.get("name", "")))
    module_source = read_text(repo / "kafa" / "__init__.py")
    version_source = read_text(repo / "kafa" / "version.py")
    derived_module_version = (
        "from .version import release_version" in module_source
        and "__version__ = release_version()" in module_source
        and ' / "VERSION"' in version_source
        and 'distribution_version("kafa")' in version_source
    )
    module_literals = re.findall(
        r"\b\d+\.\d+\.\d+(?:-beta\.\d+|b\d+)?\b",
        module_source,
    )
    add_check(
        checks,
        "module version derivation",
        derived_module_version and not module_literals,
        f"derived={derived_module_version} literals={module_literals}",
    )

    runtime_identity = repo / "plugins" / "codex-project-harness" / "core" / "__init__.py"
    runtime_version = read_python_constant(runtime_identity, "RUNTIME_VERSION")
    kernel_version = read_python_constant(runtime_identity, "KERNEL_VERSION")
    schema_version = read_python_constant(runtime_identity, "SCHEMA_VERSION")
    add_check(
        checks,
        "runtime version alignment",
        runtime_version == manifest.get("runtime_version"),
        f"runtime={runtime_version} manifest={manifest.get('runtime_version')}",
    )
    add_check(
        checks,
        "kernel version alignment",
        kernel_version == manifest.get("kernel_version"),
        f"kernel={kernel_version} manifest={manifest.get('kernel_version')}",
    )
    add_check(
        checks,
        "schema version alignment",
        schema_version == manifest.get("schema_version_runtime"),
        f"runtime={schema_version} manifest={manifest.get('schema_version_runtime')}",
    )

    readme = read_text(repo / "README.md")
    current_intro = readme.split("\n## ", 1)[0]
    docs_aligned = (
        f"v{version}" in current_intro
        and f"Kernel v{manifest.get('kernel_version')}" in current_intro
        and f"schema {manifest.get('schema_version_runtime')}" in readme
    )
    add_check(
        checks,
        "current documentation runtime facts",
        docs_aligned,
        (
            f"version=v{version} kernel={manifest.get('kernel_version')} "
            f"schema={manifest.get('schema_version_runtime')}"
        ),
    )

    changelog = read_text(repo / "CHANGELOG.md")
    development_heading = f"## {tag} - Unreleased"
    release_heading = re.compile(rf"^## {re.escape(tag)} - \d{{4}}-\d{{2}}-\d{{2}}$", re.MULTILINE)
    changelog_ok = development_heading in changelog if state == "development" else bool(release_heading.search(changelog))
    add_check(checks, "release notes heading", changelog_ok, development_heading if state == "development" else f"dated {tag} heading")
    section = changelog_section(changelog, tag)
    add_check(checks, "release notes content", "### " in section and len(section.splitlines()) >= 4, f"{len(section.splitlines())} lines")
    required_release_facts = [
        f"schema {manifest.get('schema_version_runtime')}",
        str(manifest.get("runtime_version", "")),
    ]
    missing_release_facts = [fact for fact in required_release_facts if fact and fact.lower() not in section.lower()]
    expected_schema = int(manifest.get("schema_version_runtime", 0) or 0)
    schema_claims = {int(value) for value in re.findall(r"\bschema\s+`?(\d+)`?", section, re.IGNORECASE)}
    unexpected_schema_claims = sorted(schema_claims - {expected_schema})
    add_check(
        checks,
        "release notes runtime facts",
        not missing_release_facts and not unexpected_schema_claims,
        f"missing={missing_release_facts} unexpected_schema_claims={unexpected_schema_claims}",
    )

    tag_points_at = git_output(repo, ["tag", "--points-at", "HEAD"]).splitlines()
    if state == "development":
        matching_tags = git_output(repo, ["tag", "--list", tag]).splitlines()
        add_check(checks, "development tag absence", tag not in matching_tags, f"matching tags={matching_tags}")
    if require_tag:
        add_check(checks, "tag release state", state == "release", state)
        add_check(checks, "tag points at HEAD", tag in tag_points_at, f"tags at HEAD={tag_points_at}")
        worktree_status = git_output(repo, ["status", "--porcelain", "--untracked-files=all"])
        add_check(checks, "tag worktree clean", not worktree_status, worktree_status or "clean")
        ref_name = os.environ.get("GITHUB_REF_NAME", "")
        if ref_name:
            add_check(checks, "workflow tag", ref_name == tag, f"workflow={ref_name} manifest={tag}")

    return {"ok": all(check["ok"] for check in checks), "repo": str(repo), "manifest": manifest, "checks": checks}


def changelog_section(text: str, tag: str) -> str:
    match = re.search(rf"^## {re.escape(tag)}(?:\s+-.*)?$", text, re.MULTILINE)
    if not match:
        return ""
    next_heading = re.search(r"^## ", text[match.end():], re.MULTILINE)
    end = match.end() + next_heading.start() if next_heading else len(text)
    return text[match.start():end].strip()


def read_python_constant(path: Path, name: str) -> Any:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    resolved: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            target_names = [target.id for target in targets if isinstance(target, ast.Name)]
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                value = resolved.get(node.value.id) if isinstance(node.value, ast.Name) else None
            for target_name in target_names:
                resolved[target_name] = value
    return resolved.get(name)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def read_toml(path: Path) -> dict[str, Any]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def git_output(repo: Path, args: list[str]) -> str:
    completed = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    return completed.stdout.strip() if completed.returncode == 0 else ""


def add_check(checks: list[dict[str, Any]], name: str, ok: bool, details: str) -> None:
    checks.append({"name": name, "ok": bool(ok), "details": details})


if __name__ == "__main__":
    raise SystemExit(main())
