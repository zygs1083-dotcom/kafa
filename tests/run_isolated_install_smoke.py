#!/usr/bin/env python3
"""Run a real wheel, Codex plugin, cache hook, doctor, and removal smoke."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import venv
from contextlib import closing
from pathlib import Path
from typing import Any

SCRIPT_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_REPO_ROOT))

from kafa.codex_app_server import (
    APPROVED_AGENT_TEMPLATES,
    AppServerClient,
    validate_app_server_discovery,
)


PLUGIN_ID = "codex-project-harness@kafa-local"


SCHEMA30_SEED_SCRIPT = r'''from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

plugin_root = Path(sys.argv[1]).resolve()
project_root = Path(sys.argv[2]).resolve()
sys.path[:0] = [str(plugin_root / "scripts"), str(plugin_root)]

import harness_db
from core import schema_lifecycle

loaded = {
    "schema_lifecycle": str(Path(schema_lifecycle.__file__).resolve()),
    "harness_db": str(Path(harness_db.__file__).resolve()),
}
for label, value in loaded.items():
    if not Path(value).is_relative_to(plugin_root):
        raise RuntimeError(f"artifact migration loaded {label} outside installed cache: {value}")

database = project_root / ".ai-team/state/harness.db"
database.parent.mkdir(parents=True, exist_ok=True)
timestamp = "2026-07-21T00:00:00Z"
cycle_id = "CYCLE-artifact-migration"
with closing(sqlite3.connect(database)) as conn:
    conn.execute("pragma foreign_keys = on")
    conn.execute("begin immediate")
    schema_lifecycle.create_schema30(conn)
    conn.execute(
        """
        insert into project
        (id, project_id, schema_version, runtime_version, phase, current_cycle_id,
         status, scope_status, current_owner, revision, updated_at)
        values (1, 'artifact-migration', ?, ?, 'intake', ?, 'draft',
                'unconfirmed', 'root-controller', 1, ?)
        """,
        (
            schema_lifecycle.SCHEMA30_VERSION,
            schema_lifecycle.SCHEMA30_RUNTIME_VERSION,
            cycle_id,
            timestamp,
        ),
    )
    conn.execute(
        """
        insert into delivery_cycles
        (id, name, goal, status, phase, base_ref, candidate_sha, started_at,
         closed_at, created_at, updated_at)
        values (?, 'Artifact migration', 'preserve installed schema 30 facts',
                'active', 'intake', '', ?, ?, '', ?, ?)
        """,
        (cycle_id, "a" * 64, timestamp, timestamp, timestamp),
    )
    conn.execute(
        """
        insert into requirements
        (id, cycle_id, kind, body, priority, status, revision, updated_at)
        values ('REQ-artifact', ?, 'functional',
                'preserve artifact migration requirement', 'must', 'active', 1, ?)
        """,
        (cycle_id, timestamp),
    )
    conn.execute(
        """
        insert into acceptance
        (id, cycle_id, criterion, priority, status, revision)
        values ('AC-artifact', ?, 'preserve artifact migration acceptance',
                'must', 'active', 1)
        """,
        (cycle_id,),
    )
    conn.execute(
        "insert into requirement_acceptance values (?, 'REQ-artifact', 'AC-artifact')",
        (cycle_id,),
    )
    conn.execute(
        """
        insert into failure_modes
        (id, cycle_id, feature, scenario, trigger, expected_behavior, recovery,
         data_safety, risk, status, accepted_by, acceptance_reason,
         acceptance_scope, accepted_revision, expires_at, revision)
        values ('FM-artifact', ?, 'migration', 'copy failure', 'migration',
                'rollback', 'restore verified backup', 'no data loss', 'low',
                'active', '', '', '', null, '', 1)
        """,
        (cycle_id,),
    )
    conn.execute(
        "insert into failure_mode_acceptance values (?, 'FM-artifact', 'AC-artifact')",
        (cycle_id,),
    )
    conn.commit()

(project_root / ".gitignore").write_text(
    "\n".join(harness_db.RUNTIME_GITIGNORE_PATTERNS) + "\n",
    encoding="utf-8",
)
print(json.dumps({
    **loaded,
    "database": str(database),
    "source_schema": schema_lifecycle.SCHEMA30_VERSION,
    "source_runtime": schema_lifecycle.SCHEMA30_RUNTIME_VERSION,
}, sort_keys=True))
'''


def read_quickstart_facts(database: Path) -> tuple[tuple[int, int, int, int], str]:
    with closing(sqlite3.connect(database)) as conn:
        facts = tuple(
            int(conn.execute(f"select count(*) from {table}").fetchone()[0])
            for table in ("executions", "validations", "quality_gates", "deliveries")
        )
        task_status = str(
            conn.execute(
                "select status from tasks where id='INSTALL-T1'"
            ).fetchone()[0]
        )
    return facts, task_status


def doctor_plugin_digests(
    checks: dict[str, dict[str, Any]],
    expected_cache_root: Path,
) -> dict[str, str]:
    content_details = str(checks.get("installed plugin content", {}).get("details", ""))
    cache_details = str(checks.get("codex plugin cache", {}).get("details", ""))
    content_match = re.fullmatch(
        r"installed=(?P<managed>[0-9a-f]{64}) source=(?P<source>[0-9a-f]{64})",
        content_details,
    )
    cache_match = re.fullmatch(
        r"path=(?P<path>.+?) cache=(?P<cache>[0-9a-f]{64}) "
        r"installed=(?P<managed>[0-9a-f]{64})",
        cache_details,
    )
    if content_match is None or cache_match is None:
        raise RuntimeError(
            "installed doctor did not publish parseable plugin digests: "
            f"content={content_details!r} cache={cache_details!r}"
        )
    reported_cache_root = Path(cache_match.group("path")).resolve()
    if reported_cache_root != expected_cache_root.resolve():
        raise RuntimeError(
            "installed doctor cache path mismatch: "
            f"reported={reported_cache_root} expected={expected_cache_root.resolve()}"
        )
    digests = {
        "plugin_source_tree_sha256": content_match.group("source"),
        "managed_plugin_tree_sha256": content_match.group("managed"),
        "cache_plugin_tree_sha256": cache_match.group("cache"),
    }
    if cache_match.group("managed") != digests["managed_plugin_tree_sha256"]:
        raise RuntimeError("installed doctor reported inconsistent managed plugin digests")
    if len(set(digests.values())) != 1:
        raise RuntimeError(f"installed plugin digest mismatch: {digests}")
    return {
        **digests,
        "cache_plugin_path": str(reported_cache_root),
    }


def discover_with_app_server(codex: str, *, env: dict[str, str], cwd: Path) -> dict[str, Any]:
    client = AppServerClient(codex_command(codex, "app-server", "--stdio"), env=env, cwd=cwd)
    try:
        initialized = client.request(
            "initialize",
            {
                "clientInfo": {"name": "kafa-isolated-install-smoke", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        client.notify("initialized", {})
        return {
            "initialize": initialized,
            "skills": client.request("skills/list", {"cwds": [str(cwd)], "forceReload": True}),
            "hooks": client.request("hooks/list", {"cwds": [str(cwd)]}),
            "plugin": client.request("plugin/installed", {"cwds": [str(cwd)]}),
            "notifications": client.notifications,
        }
    finally:
        client.close()


def artifact_installed_migration_evidence(
    root: Path,
    *,
    cache_root: Path,
    installed_harness: Path,
    venv_python: Path,
    kafa: list[str],
    env: dict[str, str],
    target_schema: int,
    target_runtime: str,
) -> dict[str, Any]:
    migration_repo = root / "migration-business"
    migration_repo.mkdir()
    run(["git", "init"], env=env, cwd=migration_repo)
    seed_script = root / "seed_artifact_schema30.py"
    seed_script.write_text(SCHEMA30_SEED_SCRIPT.lstrip(), encoding="utf-8")
    seed = json.loads(
        run(
            [str(venv_python), str(seed_script), str(cache_root), str(migration_repo)],
            env=env,
            cwd=root,
        )
    )
    for field in ("schema_lifecycle", "harness_db"):
        loaded_path = Path(str(seed.get(field, ""))).resolve()
        if not loaded_path.is_relative_to(cache_root.resolve()):
            raise RuntimeError(
                f"artifact migration loaded {field} outside installed cache: {loaded_path}"
            )
    if seed.get("source_schema") != 30 or seed.get("source_runtime") != "5.0.0":
        raise RuntimeError(f"artifact schema 30 seed identity mismatch: {seed}")

    database = migration_repo / ".ai-team/state/harness.db"
    source_sha256 = sha256_file(database)
    dry_run = run(
        [
            str(venv_python),
            str(installed_harness),
            "--root",
            str(migration_repo),
            "migrate",
            "--from-version",
            "30",
            "--to-version",
            str(target_schema),
            "--dry-run",
        ],
        env=env,
        cwd=migration_repo,
    )
    if f"DRY-RUN: would migrate 30->{target_schema}" not in dry_run:
        raise RuntimeError(f"artifact migration dry-run output mismatch: {dry_run}")
    if sha256_file(database) != source_sha256:
        raise RuntimeError("artifact migration dry-run changed the schema 30 database")
    if (migration_repo / ".ai-team/backups").exists():
        raise RuntimeError("artifact migration dry-run created a backup")
    if (migration_repo / ".ai-team/state/local-core-migration.lock").exists():
        raise RuntimeError("artifact migration dry-run left a migration sentinel")
    if (migration_repo / ".ai-team/control/project-state.yaml").exists():
        raise RuntimeError("artifact migration dry-run published production projections")

    migrated = run(
        [
            str(venv_python),
            str(installed_harness),
            "--root",
            str(migration_repo),
            "migrate",
            "--from-version",
            "30",
            "--to-version",
            str(target_schema),
        ],
        env=env,
        cwd=migration_repo,
    )
    if f"OK: migrated 30->{target_schema}" not in migrated:
        raise RuntimeError(f"artifact migration output mismatch: {migrated}")
    harness_doctor = run(
        [
            str(venv_python),
            str(installed_harness),
            "--root",
            str(migration_repo),
            "doctor",
        ],
        env=env,
        cwd=migration_repo,
    )
    if "OK: harness doctor passed" not in harness_doctor:
        raise RuntimeError(f"artifact migration harness doctor failed: {harness_doctor}")
    project_doctor = json.loads(
        run(
            [*kafa, "project", "doctor", "--repo", str(migration_repo), "--json"],
            env=env,
            cwd=migration_repo,
        )
    )
    if project_doctor.get("ok") is not True:
        raise RuntimeError(f"artifact migration public doctor failed: {project_doctor}")

    manifests = sorted(
        (migration_repo / ".ai-team/backups").glob("**/migration-manifest.json")
    )
    if len(manifests) != 1:
        raise RuntimeError(
            f"artifact migration must publish one manifest, found {len(manifests)}"
        )
    manifest_path = manifests[0]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    projection_backup = manifest.get("projection_backup", {})
    if (
        manifest.get("status") != "activated"
        or manifest.get("source_version") != 30
        or manifest.get("target_version") != target_schema
        or manifest.get("projection_restore_status") != "not-needed"
        or not isinstance(projection_backup, dict)
        or projection_backup.get("live_projection_count") != 13
        or projection_backup.get("rollback_path_count") != 14
    ):
        raise RuntimeError(f"artifact migration manifest contract failed: {manifest}")
    backup = manifest.get("backup", {})
    if not isinstance(backup, dict):
        raise RuntimeError("artifact migration manifest backup is not an object")
    backup_path = Path(str(backup.get("backup_path", "")))
    backup_sha256 = str(backup.get("sha256", ""))
    if (
        not backup_path.is_file()
        or not backup_path.resolve().is_relative_to(migration_repo.resolve())
        or sha256_file(backup_path) != backup_sha256
    ):
        raise RuntimeError(f"artifact migration backup verification failed: {backup}")

    with closing(sqlite3.connect(database)) as conn:
        project = conn.execute(
            "select schema_version, runtime_version from project where id=1"
        ).fetchone()
        cycle_count = int(
            conn.execute(
                "select count(*) from delivery_cycles where id='CYCLE-artifact-migration'"
            ).fetchone()[0]
        )
        requirement = conn.execute(
            "select body from requirements where id='REQ-artifact'"
        ).fetchone()
        acceptance = conn.execute(
            "select criterion from acceptance where id='AC-artifact'"
        ).fetchone()
        links = (
            int(conn.execute("select count(*) from requirement_acceptance").fetchone()[0]),
            int(conn.execute("select count(*) from failure_mode_acceptance").fetchone()[0]),
        )
        normalized_failure_mode = conn.execute(
            "select status from failure_modes where id='FM-artifact'"
        ).fetchone()
        invented_authority_count = sum(
            int(conn.execute(f"select count(*) from {table}").fetchone()[0])
            for table in (
                "acceptance_target_qualifications",
                "quality_gate_qualifications",
                "outcome_observations",
            )
        )
        migration_row = conn.execute(
            "select from_version, to_version, status from migrations "
            "where from_version=30 and to_version=? order by id desc limit 1",
            (target_schema,),
        ).fetchone()
        foreign_key_issues = conn.execute("pragma foreign_key_check").fetchall()
    with closing(sqlite3.connect(backup_path)) as backup_conn:
        backup_schema = int(
            backup_conn.execute(
                "select schema_version from project where id=1"
            ).fetchone()[0]
        )

    if project != (target_schema, target_runtime):
        raise RuntimeError(f"artifact migration project identity mismatch: {project}")
    if (
        cycle_count != 1
        or requirement != ("preserve artifact migration requirement",)
        or acceptance != ("preserve artifact migration acceptance",)
        or links != (1, 1)
        or normalized_failure_mode != ("identified",)
        or invented_authority_count != 0
        or migration_row != (30, target_schema, "activated")
        or foreign_key_issues
        or backup_schema != 30
    ):
        raise RuntimeError(
            "artifact migration fact preservation failed: "
            f"cycle={cycle_count} requirement={requirement} acceptance={acceptance} "
            f"links={links} failure_mode={normalized_failure_mode} "
            f"invented={invented_authority_count} migration={migration_row} "
            f"foreign_keys={foreign_key_issues} backup_schema={backup_schema}"
        )

    return {
        "installed_migration_dry_run_ok": True,
        "installed_migration_ok": True,
        "installed_migration_doctor_ok": True,
        "installed_migration_backup_ok": True,
        "installed_migration_source_version": 30,
        "installed_migration_target_version": target_schema,
        "installed_migration_normalized_failure_modes": 1,
        "installed_migration_invented_authority_count": invented_authority_count,
        "installed_migration_manifest_sha256": sha256_file(manifest_path),
        "installed_migration_backup_sha256": backup_sha256,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--codex-bin", default="")
    parser.add_argument("--wheel", default="")
    parser.add_argument("--source-archive", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    source_repo = Path(args.repo).expanduser().resolve()
    try:
        report = run_smoke(source_repo, args.codex_bin, args.wheel, args.source_archive)
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        else:
            print(f"ERROR: {exc}")
        return 1
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"OK: isolated install smoke passed with {report['codex_version']}")
    return 0


def run_smoke(
    source_repo: Path,
    codex_value: str = "",
    wheel_value: str = "",
    source_archive_value: str = "",
) -> dict[str, Any]:
    manifest = json.loads((source_repo / "release.json").read_text(encoding="utf-8"))
    version = str(manifest["version"])
    pep440_version = str(manifest["pep440_version"])
    runtime_version = str(manifest["runtime_version"])
    schema_version = int(manifest["schema_version_runtime"])
    expected_codex_version = f"codex-cli {manifest['codex_cli_smoke_version']}"
    codex = str(Path(codex_value).expanduser().resolve()) if codex_value else shutil.which("codex")
    if not codex:
        raise RuntimeError("pinned Codex CLI is not on PATH")

    clean_env = os.environ.copy()
    clean_env.pop("PYTHONPATH", None)
    if bool(wheel_value) != bool(source_archive_value):
        raise RuntimeError("--wheel and --source-archive must be provided together")
    artifact_mode = bool(wheel_value)
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        if artifact_mode:
            wheel = Path(wheel_value).expanduser().resolve()
            source_archive = Path(source_archive_value).expanduser().resolve()
            if not wheel.is_file() or not source_archive.is_file():
                raise RuntimeError(f"release artifacts missing: wheel={wheel} source={source_archive}")
            artifact_inputs = validate_artifact_inputs(
                wheel,
                source_archive,
                pep440_version,
            )
            release_repo = extract_source_archive(source_archive, root / "release-artifact")
        else:
            release_repo = root / "release-source"
            shutil.copytree(
                source_repo,
                release_repo,
                ignore=shutil.ignore_patterns(".git", ".venv", ".ai-team", "build", "*.egg-info", "__pycache__", "*.pyc"),
            )
            dist = root / "dist"
            dist.mkdir()
            run(
                [sys.executable, "-m", "pip", "wheel", "--no-deps", ".", "--wheel-dir", str(dist)],
                env=clean_env,
                cwd=release_repo,
            )
            wheel = next(dist.glob("kafa-*.whl"))
            artifact_inputs = {
                "wheel_name": wheel.name,
                "wheel_sha256": sha256_file(wheel),
                "source_archive_name": None,
                "source_archive_sha256": None,
            }
        artifact_manifest = json.loads((release_repo / "release.json").read_text(encoding="utf-8"))
        for field in ["version", "pep440_version", "tag", "package", "plugin"]:
            if artifact_manifest.get(field) != manifest.get(field):
                raise RuntimeError(
                    f"source artifact manifest mismatch for {field}: "
                    f"artifact={artifact_manifest.get(field)} checkout={manifest.get(field)}"
                )

        venv_root = root / "venv"
        venv.EnvBuilder(with_pip=True).create(venv_root)
        venv_python = venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run([str(venv_python), "-m", "pip", "install", "--no-deps", str(wheel)], env=clean_env, cwd=root)
        module_info = json.loads(
            run(
                [
                    str(venv_python),
                    "-c",
                    "import json, kafa; print(json.dumps({'path': kafa.__file__, 'version': kafa.__version__}))",
                ],
                env=clean_env,
                cwd=root,
            )
        )
        imported_from = Path(module_info["path"]).resolve()
        if not imported_from.is_relative_to(venv_root.resolve()):
            raise RuntimeError(f"wheel smoke imported checkout module: {imported_from}")
        if module_info["version"] != version:
            raise RuntimeError(f"wheel version mismatch: actual={module_info['version']} expected={version}")
        codex_version = run(codex_command(codex, "--version"), env=clean_env, cwd=root).strip()
        if codex_version != expected_codex_version:
            raise RuntimeError(f"Codex CLI version mismatch: actual={codex_version} expected={expected_codex_version}")

        env = clean_env.copy()
        env["HOME"] = str(root / "home")
        env["CODEX_HOME"] = str(root / "codex-home")
        env["PATH"] = str(venv_python.parent) + os.pathsep + env.get("PATH", "")
        Path(env["HOME"]).mkdir()
        Path(env["CODEX_HOME"]).mkdir()
        kafa = [str(venv_python), "-m", "kafa.cli"]

        run([*kafa, "plugin", "install", "--scope", "user", "--repo", str(release_repo)], env=env, cwd=root)
        marketplaces = json.loads(run(codex_command(codex, "plugin", "marketplace", "list", "--json"), env=env, cwd=root))
        if not any(item.get("name") == "kafa-local" for item in marketplaces["marketplaces"]):
            raise RuntimeError(f"personal marketplace not discovered: {marketplaces}")
        available = json.loads(run(codex_command(codex, "plugin", "list", "--available", "--json"), env=env, cwd=root))
        if not any(item.get("pluginId") == "codex-project-harness@kafa-local" for item in available["available"]):
            raise RuntimeError(f"plugin not available: {available}")

        added = json.loads(run(codex_command(codex, "plugin", "add", PLUGIN_ID, "--json"), env=env, cwd=root))
        cache_root = Path(added["installedPath"])
        installed = json.loads(run(codex_command(codex, "plugin", "list", "--json"), env=env, cwd=root))["installed"]
        if not any(
            item.get("pluginId") == PLUGIN_ID
            and item.get("installed") is True
            and item.get("enabled") is True
            for item in installed
        ):
            raise RuntimeError(f"plugin not installed and enabled: {installed}")

        business_repo = root / "business"
        business_repo.mkdir()
        run(["git", "init"], env=env, cwd=business_repo)
        discovery = discover_with_app_server(codex, env=env, cwd=business_repo)
        discovery_report = validate_app_server_discovery(
            discovery,
            cache_root=cache_root,
            plugin_id=PLUGIN_ID,
            version=version,
        )

        project_init = run(
            [*kafa, "project", "init", "--repo", str(business_repo)],
            env=env,
            cwd=business_repo,
        )
        project_status = run(
            [*kafa, "project", "status", "--repo", str(business_repo)],
            env=env,
            cwd=business_repo,
        )
        if (
            f"schema_version: {schema_version}" not in project_status
            or f"runtime_version: {runtime_version}" not in project_status
        ):
            raise RuntimeError(
                "installed project status does not match release.json: "
                f"{project_status}"
            )
        installed_templates = {
            path.name for path in (business_repo / ".codex/agents").glob("*.toml")
        }
        if installed_templates != APPROVED_AGENT_TEMPLATES:
            raise RuntimeError(
                f"project agent template inventory mismatch: actual={sorted(installed_templates)} "
                f"expected={sorted(APPROVED_AGENT_TEMPLATES)}"
            )

        (business_repo / "test_quickstart.py").write_text(
            "import unittest\n\nclass InstalledTest(unittest.TestCase):\n"
            "    def test_installed_runtime(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        installed_harness = cache_root / "scripts" / "harness.py"
        quickstart = run(
            [str(venv_python), str(installed_harness), "--root", str(business_repo), "quickstart", "minimal", "--id", "INSTALL", "--goal", "verify installed runtime", "--acceptance", "installed test passes", "--task", "run installed verification", "--test-command", "python -m unittest test_quickstart.py", "--execute"],
            env=env,
            cwd=business_repo,
        )
        quickstart_status = json.loads(run(
            [str(venv_python), str(installed_harness), "--root", str(business_repo), "quickstart", "status", "--json"],
            env=env,
            cwd=business_repo,
        ))
        quickstart_facts, quickstart_task_status = read_quickstart_facts(
            business_repo / ".ai-team/state/harness.db"
        )
        if "OK: quickstart minimal verified setup INSTALL" not in quickstart or quickstart_facts != (1, 1, 0, 0) or quickstart_task_status != "submitted" or quickstart_status["ready_for_delivery"] or "controller_execution" in quickstart_status["missing"]:
            raise RuntimeError(f"installed quickstart contract failed: facts={quickstart_facts} task={quickstart_task_status} status={quickstart_status} output={quickstart}")

        migration_evidence = artifact_installed_migration_evidence(
            root,
            cache_root=cache_root,
            installed_harness=installed_harness,
            venv_python=venv_python,
            kafa=kafa,
            env=env,
            target_schema=schema_version,
            target_runtime=runtime_version,
        )

        doctor = json.loads(run([*kafa, "doctor", "--scope", "user", "--repo", str(release_repo), "--json"], env=env, cwd=root))
        if doctor.get("ok") is not True:
            raise RuntimeError(f"installed doctor failed: {doctor}")
        checks = {item["name"]: item for item in doctor["checks"]}
        for name in ["hook definition", "codex plugin registration", "codex plugin cache"]:
            if checks.get(name, {}).get("ok") is not True:
                raise RuntimeError(f"installed doctor check failed: {name}: {checks.get(name)}")
        plugin_digests = doctor_plugin_digests(checks, cache_root)

        hook = json.loads((cache_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]["SessionStart"][0]["hooks"][0]
        command = hook["commandWindows"] if os.name == "nt" else hook["command"]
        hook_env = env.copy()
        hook_env["PLUGIN_ROOT"] = str(cache_root)
        hook_result = subprocess.run(
            command,
            input=json.dumps({"source": "ci-install-smoke"}),
            text=True,
            capture_output=True,
            cwd=business_repo,
            env=hook_env,
            shell=True,
            check=False,
        )
        if hook_result.returncode != 0 or f"version: {version}" not in hook_result.stdout:
            raise RuntimeError(f"installed cache hook failed: {hook_result.stdout}{hook_result.stderr}")

        run(codex_command(codex, "plugin", "remove", PLUGIN_ID, "--json"), env=env, cwd=root)
        removed = json.loads(run(codex_command(codex, "plugin", "list", "--json"), env=env, cwd=root))["installed"]
        if any(item.get("pluginId") == PLUGIN_ID for item in removed):
            raise RuntimeError(f"plugin remained installed after removal: {removed}")
        codex_cache_removed = not os.path.lexists(str(cache_root))
        if not codex_cache_removed:
            raise RuntimeError(f"Codex plugin cache remained after removal: {cache_root}")

        managed_plugin = Path(env["HOME"]) / ".agents/plugins/codex-project-harness"
        marketplace_path = Path(env["HOME"]) / ".agents/plugins/marketplace.json"
        uninstall_output = run(
            [
                *kafa,
                "plugin",
                "uninstall",
                "--scope",
                "user",
                "--repo",
                str(release_repo),
                "--remove-files",
            ],
            env=env,
            cwd=root,
        )
        marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
        marketplace_entry_removed = not any(
            isinstance(item, dict) and item.get("name") == "codex-project-harness"
            for item in marketplace.get("plugins", [])
        )
        managed_plugin_removed = not os.path.lexists(str(managed_plugin))
        installed_after_uninstall = json.loads(
            run(codex_command(codex, "plugin", "list", "--json"), env=env, cwd=root)
        )["installed"]
        available_after_uninstall = json.loads(
            run(
                codex_command(codex, "plugin", "list", "--available", "--json"),
                env=env,
                cwd=root,
            )
        )["available"]
        codex_unregister_ok = not any(
            item.get("pluginId") == PLUGIN_ID for item in installed_after_uninstall
        )
        if (
            not marketplace_entry_removed
            or not managed_plugin_removed
            or not codex_unregister_ok
            or any(item.get("pluginId") == PLUGIN_ID for item in available_after_uninstall)
            or "removed 1 marketplace entry" not in uninstall_output
            or "removed copied plugin" not in uninstall_output
        ):
            raise RuntimeError(
                "full Kafa uninstall failed: "
                f"marketplace_entry_removed={marketplace_entry_removed} "
                f"managed_plugin_removed={managed_plugin_removed} "
                f"codex_unregister_ok={codex_unregister_ok} "
                f"available={available_after_uninstall} output={uninstall_output!r}"
            )

        return {
            "ok": True,
            "version": version,
            "codex_version": codex_version,
            "wheel": wheel.name,
            **artifact_inputs,
            "artifact_mode": artifact_mode,
            "imported_from_venv": True,
            "marketplace_discovered": True,
            "plugin_enabled": True,
            "app_server_discovery_ok": True,
            "app_server_plugin": discovery_report["plugin_id"],
            "app_server_plugin_version": discovery_report["plugin_local_version"],
            "app_server_skill_count": discovery_report["skill_count"],
            "app_server_skills": discovery_report["skill_names"],
            "app_server_hook_count": discovery_report["hook_count"],
            "app_server_hook_events": discovery_report["hook_events"],
            "app_server_template_count": discovery_report["template_count"],
            "app_server_templates": discovery_report["template_names"],
            "app_server_runtime_script_count": discovery_report["runtime_script_count"],
            "app_server_schema_count": discovery_report["schema_count"],
            "app_server_runtime_anchor_count": discovery_report["runtime_anchor_count"],
            "retired_runtime_absent": discovery_report["retired_runtime_absent"],
            "project_init_ok": "OK: project harness initialized" in project_init,
            "project_status_ok": True,
            "installed_quickstart_ok": True,
            "installed_quickstart_task_status": quickstart_task_status,
            **migration_evidence,
            "project_agent_templates": sorted(installed_templates),
            "doctor_ok": True,
            **plugin_digests,
            "cache_hook_ok": True,
            "direct_hook_handler_ok": True,
            "host_hook_execution_observed": False,
            "host_hook_execution_reason": "deterministic install smoke proves app-server discovery; host execution requires a live authenticated turn",
            "codex_unregister_ok": codex_unregister_ok,
            "codex_cache_removed": codex_cache_removed,
            "marketplace_entry_removed": marketplace_entry_removed,
            "managed_plugin_removed": managed_plugin_removed,
            "full_uninstall_ok": True,
            "remove_ok": True,
        }


def run(command: list[str], *, env: dict[str, str], cwd: Path) -> str:
    completed = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stdout + completed.stderr)
    return completed.stdout


def codex_command(codex: str, *args: str, platform_name: str | None = None) -> list[str]:
    platform_name = os.name if platform_name is None else platform_name
    if platform_name == "nt" and Path(codex).suffix.lower() in {".cmd", ".bat"}:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", codex, *args]
    return [codex, *args]


def validate_artifact_inputs(
    wheel: Path,
    source_archive: Path,
    pep440_version: str,
) -> dict[str, str]:
    if (
        wheel.is_symlink()
        or source_archive.is_symlink()
        or not wheel.is_file()
        or not source_archive.is_file()
    ):
        raise RuntimeError("release artifact inputs must be regular files")
    expected = {
        "wheel": f"kafa-{pep440_version}-py3-none-any.whl",
        "source": f"kafa-{pep440_version}.tar.gz",
    }
    if wheel.name != expected["wheel"] or source_archive.name != expected["source"]:
        raise RuntimeError(
            "release artifact names mismatch: "
            f"wheel={wheel.name} source={source_archive.name} expected={expected}"
        )
    return {
        "wheel_name": wheel.name,
        "wheel_sha256": sha256_file(wheel),
        "source_archive_name": source_archive.name,
        "source_archive_sha256": sha256_file(source_archive),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_source_archive(archive: Path, target: Path) -> Path:
    target.mkdir(parents=True)
    target_root = target.resolve()
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        for member in members:
            destination = (target / member.name).resolve()
            if (
                not destination.is_relative_to(target_root)
                or member.issym()
                or member.islnk()
                or not (member.isfile() or member.isdir())
            ):
                raise RuntimeError(f"unsafe source archive member: {member.name}")
        bundle.extractall(target)
    manifests = list(target.glob("*/release.json"))
    if len(manifests) != 1:
        raise RuntimeError(f"source archive must contain one release root, found {len(manifests)}")
    return manifests[0].parent


if __name__ == "__main__":
    raise SystemExit(main())
