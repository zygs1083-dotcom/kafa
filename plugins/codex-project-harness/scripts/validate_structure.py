#!/usr/bin/env python3
"""Validate the Codex Project Harness plugin structure."""

from __future__ import annotations

import ast
import json
import sys
import tomllib
from pathlib import Path

from harness_lib import (
    DistributionManifestError,
    distribution_inventory_issues,
    hook_definition_issues,
    load_distribution_manifest,
)

RETIRED_CORE_FILES = ["agent_provider.py", "agent_runner.py", "connector_trust.py"]
FORBIDDEN_RUNTIME_LITERALS = [
    "gh api",
    "api.github.com",
    "api.linear.app",
    "api.notion.com",
    "api.figma.com",
    "slack.com/api",
    "github_token",
    "gh_token",
    "linear_api_key",
    "notion_token",
    "figma_token",
    "slack_bot_token",
    "harness_connector_key",
]
FORBIDDEN_PROVIDER_IMPORTS = {"github", "linear", "notion_client", "figma", "slack_sdk", "openai_codex"}

def pep440_version(release_version: str) -> str:
    marker = "-beta."
    if marker in release_version:
        base, beta = release_version.split(marker, 1)
        return f"{base}b{beta}"
    return release_version


def python_constants(path: Path) -> dict[str, object]:
    """Read literal and same-module alias constants without importing code."""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return {}
    resolved: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        try:
            value: object = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            value = resolved.get(node.value.id) if isinstance(node.value, ast.Name) else None
        for target in targets:
            if isinstance(target, ast.Name):
                resolved[target.id] = value
    return resolved


def static_runtime_domains(path: Path) -> set[str]:
    """Read top-level ``sub.add_parser`` literals without importing runtime."""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    domains: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = node.func
        if not (
            isinstance(function, ast.Attribute)
            and function.attr == "add_parser"
            and isinstance(function.value, ast.Name)
            and function.value.id == "sub"
        ):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            domains.add(first.value)
    return domains


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    repo_root = root.parent.parent
    manifest = root / ".codex-plugin" / "plugin.json"
    if not manifest.exists():
        print(f"ERROR: missing {manifest}")
        return 1

    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"ERROR: invalid plugin.json: {exc}")
        return 1

    try:
        distribution = load_distribution_manifest(root)
    except DistributionManifestError as exc:
        print(f"ERROR: {exc}")
        return 1

    errors: list[str] = []
    errors.extend(distribution_inventory_issues(root, distribution))
    if data.get("name") != "codex-project-harness":
        errors.append("plugin name must be codex-project-harness")
    version_file = repo_root / "VERSION"
    release_manifest_path = repo_root / "release.json"
    release_manifest: dict[str, object] = {}
    if release_manifest_path.exists():
        try:
            parsed_release = json.loads(release_manifest_path.read_text(encoding="utf-8"))
            if not isinstance(parsed_release, dict):
                raise ValueError("root must be an object")
            release_manifest = parsed_release
        except Exception as exc:
            errors.append(f"invalid release.json: {exc}")
    version_text = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else ""
    release_version = str(release_manifest.get("version", "") or version_text)
    if release_manifest and version_text != release_version:
        errors.append("root VERSION must match release.json")
    if release_version and data.get("version") != release_version:
        errors.append("plugin version must match release.json")
    if "schema_version" in data:
        errors.append("plugin.json must not use legacy schema_version")
    if "display_name" in data:
        errors.append("plugin.json must not use legacy display_name")
    if not isinstance(data.get("author"), dict):
        errors.append("plugin author must be an object")
    if data.get("skills") != "./skills/":
        errors.append('plugin skills must be the relative string "./skills/"')

    pyproject = repo_root / "pyproject.toml"
    if not pyproject.exists():
        errors.append("missing pyproject.toml")
    else:
        try:
            package = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"invalid pyproject.toml: {exc}")
            package = {}
        project = package.get("project", {}) if isinstance(package, dict) else {}
        scripts = project.get("scripts", {}) if isinstance(project, dict) else {}
        if project.get("name") != "kafa":
            errors.append("pyproject project.name must be kafa")
        expected_package_version = str(
            release_manifest.get("pep440_version", "") or pep440_version(release_version)
        )
        if expected_package_version and project.get("version") != expected_package_version:
            errors.append("pyproject version must match release.json")
        if project.get("requires-python") != ">=3.11":
            errors.append("pyproject requires-python must be >=3.11")
        dependencies = project.get("dependencies", [])
        if not isinstance(dependencies, list) or dependencies:
            errors.append("pyproject base dependencies must remain empty")
        optional_dependencies = project.get("optional-dependencies", {})
        if isinstance(optional_dependencies, dict):
            flattened = [str(item).lower() for values in optional_dependencies.values() if isinstance(values, list) for item in values]
            if "host-codex" in optional_dependencies or any("openai-codex" in item for item in flattened):
                errors.append("pyproject must not declare the retired Host Codex SDK dependency")
        if scripts.get("kafa") != "kafa.cli:main":
            errors.append("pyproject must expose kafa = kafa.cli:main")

    if release_manifest:
        runtime_identity = python_constants(root / "core" / "__init__.py")
        for constant, field in [
            ("RUNTIME_VERSION", "runtime_version"),
            ("KERNEL_VERSION", "kernel_version"),
            ("SCHEMA_VERSION", "schema_version_runtime"),
        ]:
            if runtime_identity.get(constant) != release_manifest.get(field):
                errors.append(
                    f"{constant} must match release.json {field}: "
                    f"runtime={runtime_identity.get(constant)!r} "
                    f"manifest={release_manifest.get(field)!r}"
                )

    interface = data.get("interface")
    if not isinstance(interface, dict):
        errors.append("plugin interface is required")
    else:
        for key in [
            "displayName",
            "shortDescription",
            "longDescription",
            "developerName",
            "category",
            "capabilities",
            "defaultPrompt",
        ]:
            if key not in interface:
                errors.append(f"plugin interface missing field: {key}")
        if "capabilities" in interface and not isinstance(interface["capabilities"], list):
            errors.append("plugin interface.capabilities must be a list")
        if "defaultPrompt" in interface and not isinstance(interface["defaultPrompt"], list):
            errors.append("plugin interface.defaultPrompt must be a list")

    for skill in distribution["skills"]:
        skill_dir = root / "skills" / skill
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            errors.append(f"missing skill file: {skill_md}")
            continue
        text = skill_md.read_text(encoding="utf-8")
        if not text.startswith("---"):
            errors.append(f"missing front matter: {skill_md}")
        if f'name: "{skill}"' not in text and f"name: {skill}" not in text:
            errors.append(f"skill name mismatch: {skill_md}")
        if "description:" not in text.split("---", 2)[1]:
            errors.append(f"missing description in front matter: {skill_md}")
        openai_yaml = skill_dir / "agents" / "openai.yaml"
        if not openai_yaml.exists():
            errors.append(f"missing skill UI metadata: {openai_yaml}")
        else:
            yaml_text = openai_yaml.read_text(encoding="utf-8")
            for required in ["interface:", "display_name:", "short_description:", "default_prompt:"]:
                if required not in yaml_text:
                    errors.append(f"openai.yaml missing {required}: {openai_yaml}")

    skill_dirs = {
        path.name for path in (root / "skills").iterdir()
        if path.name != "__pycache__"
    }
    expected_skills = set(distribution["skills"])
    for skill in sorted(skill_dirs ^ expected_skills):
        errors.append(f"skill inventory mismatch: {root / 'skills' / skill}")

    core_files = {
        path.name
        for path in (root / "core").iterdir()
        if path.is_file()
    } if (root / "core").is_dir() else set()
    expected_core = set(distribution["core"])
    for core_file in sorted(core_files ^ expected_core):
        errors.append(f"core inventory mismatch: {root / 'core' / core_file}")
    for retired in RETIRED_CORE_FILES:
        retired_path = root / "core" / retired
        if retired_path.exists():
            errors.append(f"retired core file exists: {retired_path}")
    errors.extend(local_python_import_errors(root))
    errors.extend(local_only_runtime_errors(root))

    for script in distribution["scripts"]:
        script_path = root / "scripts" / script
        if not script_path.exists():
            errors.append(f"missing runtime script: {script_path}")
    script_files = {
        path.name for path in (root / "scripts").iterdir()
        if path.is_file()
    }
    expected_scripts = set(distribution["scripts"])
    for script in sorted(script_files ^ expected_scripts):
        errors.append(f"runtime script inventory mismatch: {root / 'scripts' / script}")
    actual_domains = static_runtime_domains(root / "scripts/harness.py")
    expected_domains = set(distribution["public_runtime_domains"])
    if actual_domains != expected_domains:
        errors.append(
            "public runtime domain inventory mismatch: "
            f"actual={sorted(actual_domains)} expected={sorted(expected_domains)}"
        )

    hooks_dir = root / "hooks"
    for hook in distribution["hooks"]["files"]:
        hook_path = hooks_dir / hook
        if not hook_path.exists():
            errors.append(f"missing hook file: {hook_path}")
            continue
        if hook_path.suffix == ".json":
            try:
                hook_payload = json.loads(hook_path.read_text(encoding="utf-8"))
            except Exception as exc:
                errors.append(f"invalid hook json {hook_path}: {exc}")
            else:
                actual_events = set(hook_payload.get("hooks", {})) if isinstance(hook_payload, dict) else set()
                expected_events = set(distribution["hooks"]["events"])
                if actual_events != expected_events:
                    errors.append(
                        f"hook event inventory mismatch: actual={sorted(actual_events)} "
                        f"expected={sorted(expected_events)}"
                    )
    if hooks_dir.exists():
        hook_files = {
            path.name for path in hooks_dir.iterdir()
            if path.name != "__pycache__"
        }
        expected_hooks = set(distribution["hooks"]["files"])
        for hook in sorted(hook_files ^ expected_hooks):
            errors.append(f"hook file inventory mismatch: {hooks_dir / hook}")
    else:
        errors.append(f"missing hooks directory: {hooks_dir}")
    errors.extend(hook_definition_issues(root, distribution))

    runtime_cli = root / "skills" / "project-harness" / "scripts" / "harness.py"
    if not runtime_cli.exists():
        errors.append(f"missing project-harness self-contained CLI: {runtime_cli}")

    templates_dir = root / "templates" / "agents"
    template_files = {
        path.name for path in templates_dir.iterdir()
        if path.is_file()
    } if templates_dir.exists() else set()
    expected_agent_templates = set(distribution["templates"]["native_agents"])
    if template_files != expected_agent_templates:
        errors.append(
            f"agent template inventory mismatch: actual={sorted(template_files)} "
            f"expected={sorted(expected_agent_templates)}"
        )
    for template_name in distribution["templates"]["native_agents"]:
        template_path = templates_dir / template_name
        try:
            payload = tomllib.loads(template_path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"invalid agent template {template_path}: {exc}")
            continue
        if set(payload) != {"name", "description", "developer_instructions"}:
            errors.append(f"invalid agent template fields: {template_path}")
        if payload.get("name") != template_name.removesuffix(".toml"):
            errors.append(f"agent template name mismatch: {template_path}")

    project_templates_dir = root / "templates" / "project"
    project_template_files = {
        path.name for path in project_templates_dir.iterdir()
        if path.is_file()
    } if project_templates_dir.is_dir() else set()
    expected_project_templates = set(
        distribution["templates"]["project_support"]
    )
    if project_template_files != expected_project_templates:
        errors.append(
            "project template inventory mismatch: "
            f"actual={sorted(project_template_files)} "
            f"expected={sorted(expected_project_templates)}"
        )

    references_dir = root / "references"
    reference_files = {
        path.name for path in references_dir.iterdir()
        if path.is_file()
    } if references_dir.is_dir() else set()
    expected_references = set(distribution["references"])
    if reference_files != expected_references:
        errors.append(
            f"reference inventory mismatch: actual={sorted(reference_files)} "
            f"expected={sorted(expected_references)}"
        )

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from core.json_schema_contract import schema_definition_issues

    schema_ids: dict[str, str] = {}
    for schema in distribution["schemas"]:
        schema_path = root / "schemas" / schema
        if not schema_path.exists():
            errors.append(f"missing schema file: {schema_path}")
            continue
        try:
            schema_payload = json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"invalid schema json {schema_path}: {exc}")
            continue
        if not isinstance(schema_payload, dict):
            errors.append(f"invalid schema root {schema_path}: expected object")
            continue
        expected_id = (
            "urn:kafa:schema:31:"
            + schema.removesuffix(".schema.json")
        )
        schema_id = schema_payload.get("$id")
        if schema_id != expected_id:
            errors.append(
                f"schema id mismatch: {schema_path.name} "
                f"actual={schema_id!r} expected={expected_id!r}"
            )
        elif schema_id in schema_ids:
            errors.append(
                f"duplicate schema id: {schema_id} files="
                f"{schema_ids[schema_id]},{schema_path.name}"
            )
        else:
            schema_ids[str(schema_id)] = schema_path.name
        if schema_payload.get("additionalProperties") is not False:
            errors.append(
                f"schema additionalProperties must be explicit false: {schema_path.name}"
            )
        for issue in schema_definition_issues(schema_payload):
            errors.append(f"{schema_path.name}: {issue}")
    schema_files = {
        path.name for path in (root / "schemas").iterdir()
        if path.is_file()
    }
    expected_schemas = set(distribution["schemas"])
    for schema in sorted(schema_files ^ expected_schemas):
        errors.append(f"schema inventory mismatch: {root / 'schemas' / schema}")

    install_md = root.parent.parent / "INSTALL.md"
    if install_md.exists() and "Copy every folder under" in install_md.read_text(encoding="utf-8"):
        errors.append("INSTALL.md still documents broken copy-skills installation mode")

    stale_paths = [
        root / "skills" / "release-readiness" / "SKILL.md",
        root / "templates" / "agents" / "release-engineer.toml",
    ]
    for stale in stale_paths:
        if stale.exists():
            errors.append(f"stale delivery-only replacement still exists: {stale}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    print("OK: plugin structure is valid")
    return 0


def local_python_import_errors(root: Path) -> list[str]:
    core_root = root / "core"
    available_core = {path.stem for path in core_root.glob("*.py") if path.is_file()}
    source_paths = [
        *core_root.glob("*.py"),
        *(root / "scripts").glob("*.py"),
        *(root / "hooks").glob("*.py"),
    ]
    errors: set[str] = set()
    for path in source_paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:
            errors.add(f"invalid Python source: {path.relative_to(root)}: {exc}")
            continue
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.ImportFrom):
                if node.level and path.parent == core_root and node.module:
                    module = node.module.split(".", 1)[0]
                elif node.module and node.module.startswith("core."):
                    module = node.module.split(".", 2)[1]
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("core."):
                        module = alias.name.split(".", 2)[1]
                        if module not in available_core:
                            errors.add(
                                f"missing local Python import: core.{module} referenced by {path.relative_to(root)}"
                            )
                continue
            if module and module not in available_core:
                errors.add(f"missing local Python import: core.{module} referenced by {path.relative_to(root)}")
    return sorted(errors)


def local_only_runtime_errors(root: Path) -> list[str]:
    source_paths = [
        *(root / "core").glob("*.py"),
        *(root / "scripts").glob("*.py"),
        *(root / "hooks").glob("*.py"),
    ]
    errors: set[str] = set()
    for path in source_paths:
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(path))
        except (OSError, SyntaxError) as exc:
            errors.add(f"invalid local-only runtime source: {path.relative_to(root)}: {exc}")
            continue
        lowered = text.lower()
        if path.name != "validate_structure.py":
            for marker in FORBIDDEN_RUNTIME_LITERALS:
                if marker in lowered:
                    errors.add(f"external runtime marker {marker!r} in {path.relative_to(root)}")
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                if module.split(".", 1)[0] in FORBIDDEN_PROVIDER_IMPORTS:
                    errors.add(f"external provider import {module!r} in {path.relative_to(root)}")
    return sorted(errors)


if __name__ == "__main__":
    raise SystemExit(main())
