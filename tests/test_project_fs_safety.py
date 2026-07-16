from __future__ import annotations

import hashlib
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS_ROOT = PLUGIN_ROOT / "scripts"
for path in (PLUGIN_ROOT, SCRIPTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness_db  # noqa: E402
from core import local_core_migration  # noqa: E402
from core import schema_lifecycle  # noqa: E402
from core import execution as execution_module  # noqa: E402
from core.execution import ContainerExecutor, LocalExecutor  # noqa: E402
from core.projections import PROJECTION_PATHS, PROJECTION_ROLLBACK_PATHS  # noqa: E402
from core.store import InMemoryStore, SqliteStore, project_db_operation  # noqa: E402
from tests.test_execution_validation import (  # noqa: E402
    create_candidate,
    execution_fact_counts,
    initialize_target,
    run_harness,
)
from tests.test_local_core_hardening import _active_projection_validator  # noqa: E402
from tests.test_schema30_migration import init_schema29_fixture  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def external_identity(path: Path) -> tuple[bytes, str, int, int]:
    metadata = path.stat()
    return (
        path.read_bytes(),
        sha256(path),
        stat.S_IMODE(metadata.st_mode),
        metadata.st_ino,
    )


def project_fs(root: Path):
    from core.project_fs import ProjectFS

    return ProjectFS.open(root)


class CanonicalPathPublicRedTests(unittest.TestCase):
    def test_runtime_initialized_missing_root_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "missing-project"

            self.assertFalse(harness_db.runtime_initialized(root))
            self.assertFalse(root.exists())

    def test_doctor_missing_root_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "missing-project"

            self.assertEqual(
                harness_db.doctor(root),
                ["missing sqlite state: .ai-team/state/harness.db"],
            )
            self.assertFalse(root.exists())

    def test_status_lines_uninitialized_project_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            with self.assertRaises(Exception):
                harness_db.status_lines(root)

            self.assertFalse((root / ".ai-team").exists())

    def test_store_root_exchange_after_connect_closes_untrusted_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            replacement = base / "replacement"
            detached = base / "detached-project"
            harness_db.init_runtime(root)
            harness_db.init_runtime(replacement)
            replacement_state = replacement / ".ai-team/state"
            before = {
                path.name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                for path in replacement_state.iterdir()
                if path.is_file()
            }
            real_connect = sqlite3.connect
            opened: list[object] = []

            class TrackedConnection:
                def __init__(self, delegate) -> None:
                    object.__setattr__(self, "delegate", delegate)
                    object.__setattr__(self, "closed", False)
                    object.__setattr__(self, "commands", [])

                def __getattr__(self, name):
                    return getattr(self.delegate, name)

                def __setattr__(self, name, value) -> None:
                    if name in {"delegate", "closed", "commands"}:
                        object.__setattr__(self, name, value)
                    else:
                        setattr(self.delegate, name, value)

                def execute(self, sql, *args, **kwargs):
                    self.commands.append(str(sql))
                    return self.delegate.execute(sql, *args, **kwargs)

                def close(self) -> None:
                    if not self.closed:
                        self.delegate.close()
                        self.closed = True

            def exchange_root_then_connect(*args, **kwargs):
                root.rename(detached)
                replacement.rename(root)
                tracked = TrackedConnection(real_connect(*args, **kwargs))
                opened.append(tracked)
                return tracked

            try:
                with patch(
                    "core.store.sqlite3.connect",
                    side_effect=exchange_root_then_connect,
                ):
                    with self.assertRaisesRegex(
                        Exception,
                        "unsafe-project-path: .*path-identity-changed",
                    ):
                        with SqliteStore(root).connection():
                            self.fail("root-exchanged SQLite connection was yielded")

                self.assertEqual(len(opened), 1)
                tracked = opened[0]
                self.assertTrue(tracked.closed)
                self.assertFalse(
                    any("journal_mode" in command for command in tracked.commands)
                )
                after_state = root / ".ai-team/state"
                after = {
                    path.name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                    for path in after_state.iterdir()
                    if path.is_file()
                }
                self.assertEqual(after, before)
            finally:
                for tracked in opened:
                    tracked.close()

    def test_runtime_doctor_and_status_audit_templates_before_sqlite(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        for operation in (harness_db.doctor, harness_db.status_lines):
            with self.subTest(operation=operation.__name__), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                root = base / "project"
                harness_db.init_runtime(root)
                target = root / ".codex/agents/qa-reviewer.toml"
                target.unlink()
                external = base / "outside-qa-reviewer.toml"
                external.write_bytes(b'name = "outside"\n')
                before = external_identity(external)
                target.symlink_to(external)

                with patch(
                    "sqlite3.connect",
                    side_effect=AssertionError("SQLite must stay closed"),
                ):
                    with self.assertRaisesRegex(
                        Exception,
                        "unsafe-project-path: .codex/agents/qa-reviewer.toml",
                    ):
                        operation(root)

                self.assertEqual(external_identity(external), before)

    def test_init_rejects_symlinked_gitignore_without_mutating_referent(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            root.mkdir()
            external = base / "outside.gitignore"
            external.write_bytes(b"outside-authority\n")
            external.chmod(0o640)
            before = (external.read_bytes(), sha256(external), stat.S_IMODE(external.stat().st_mode))
            (root / ".gitignore").symlink_to(external)

            with self.assertRaisesRegex(Exception, "unsafe-project-path: .gitignore"):
                harness_db.init_runtime(root)

            after = (external.read_bytes(), sha256(external), stat.S_IMODE(external.stat().st_mode))
            self.assertEqual(after, before)
            self.assertFalse((root / ".ai-team/state/harness.db").exists())

    def test_init_rejects_symlinked_projection_before_database_creation(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            target = root / ".ai-team/control/project-state.yaml"
            target.parent.mkdir(parents=True)
            external = base / "outside-state.yaml"
            external.write_bytes(b"outside: authority\n")
            before = (external.read_bytes(), sha256(external))
            target.symlink_to(external)

            with self.assertRaisesRegex(
                Exception,
                "unsafe-project-path: .ai-team/control/project-state.yaml",
            ):
                harness_db.init_runtime(root)

            self.assertEqual((external.read_bytes(), sha256(external)), before)
            self.assertFalse((root / ".ai-team/state/harness.db").exists())

    def test_init_rejects_symlinked_agent_template_instead_of_skipping_it(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            target = root / ".codex/agents/developer.toml"
            target.parent.mkdir(parents=True)
            external = base / "outside-agent.toml"
            external.write_bytes(b'name = "outside"\n')
            before = external.read_bytes()
            target.symlink_to(external)

            with self.assertRaisesRegex(
                Exception,
                "unsafe-project-path: .codex/agents/developer.toml",
            ):
                harness_db.init_runtime(root)

            self.assertEqual(external.read_bytes(), before)
            self.assertFalse((root / ".ai-team/state/harness.db").exists())

    def test_operation_lock_rejects_symlink_without_chmod_or_write(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            lock = root / ".ai-team/state/harness.db.operation.lock"
            lock.parent.mkdir(parents=True)
            external = base / "outside.lock"
            external.write_bytes(b"outside-lock")
            external.chmod(0o644)
            before = (external.read_bytes(), sha256(external), stat.S_IMODE(external.stat().st_mode))
            lock.symlink_to(external)

            with self.assertRaisesRegex(
                Exception,
                "unsafe-project-path: .ai-team/state/harness.db.operation.lock",
            ):
                with project_db_operation(root):
                    self.fail("unsafe operation lock was acquired")

            after = (external.read_bytes(), sha256(external), stat.S_IMODE(external.stat().st_mode))
            self.assertEqual(after, before)

    def test_operation_lock_rejects_hard_linked_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            lock = root / ".ai-team/state/harness.db.operation.lock"
            lock.parent.mkdir(parents=True)
            external = base / "outside-hardlink.lock"
            external.write_bytes(b"outside-lock")
            before = external_identity(external)
            os.link(external, lock)

            with self.assertRaisesRegex(
                Exception,
                "unsafe-project-path: .ai-team/state/harness.db.operation.lock: hard-linked-target",
            ):
                with project_db_operation(root):
                    self.fail("hard-linked operation lock was acquired")

            self.assertEqual(external_identity(external), before)

    def test_nested_project_fs_open_borrows_pinned_root_and_rejects_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            root.mkdir()
            detached = base / "detached-project"
            marker = b"replacement-root-must-remain-untouched\n"

            with project_db_operation(root) as pinned_fs:
                root.rename(detached)
                root.mkdir()
                replacement_marker = root / "replacement-marker.txt"
                replacement_marker.write_bytes(marker)

                with project_fs(root) as nested_fs:
                    self.assertEqual(
                        nested_fs.root_identity_key,
                        pinned_fs.root_identity_key,
                    )
                    with self.assertRaisesRegex(
                        Exception,
                        "unsafe-project-path: .*path-identity-changed",
                    ):
                        nested_fs.audit((Path(".gitignore"),), allow_missing=True)

                self.assertEqual(replacement_marker.read_bytes(), marker)
                self.assertEqual(
                    sorted(path.name for path in root.iterdir()),
                    ["replacement-marker.txt"],
                )

    def test_store_rejects_symlinked_database_before_sqlite_open(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            harness_db.init_runtime(root)
            db = root / ".ai-team/state/harness.db"
            external = base / "outside.db"
            db.replace(external)
            db.symlink_to(external)
            before = (external.read_bytes(), sha256(external), stat.S_IMODE(external.stat().st_mode))

            with self.assertRaisesRegex(
                Exception,
                "unsafe-project-path: .ai-team/state/harness.db",
            ):
                with SqliteStore(root).connection() as conn:
                    conn.execute("select count(*) from project").fetchone()

            after = (external.read_bytes(), sha256(external), stat.S_IMODE(external.stat().st_mode))
            self.assertEqual(after, before)

    def test_store_rejects_hard_linked_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            harness_db.init_runtime(root)
            db = root / ".ai-team/state/harness.db"
            external = base / "outside-hardlink.db"
            os.link(db, external)

            with self.assertRaisesRegex(
                Exception,
                "unsafe-project-path: .ai-team/state/harness.db: hard-linked-target",
            ):
                with SqliteStore(root).connection() as conn:
                    conn.execute("select count(*) from project").fetchone()

    def test_store_rejects_symlinked_sentinel_as_unsafe_not_migration_metadata(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            state = root / ".ai-team/state"
            state.mkdir(parents=True)
            external = base / "outside-sentinel.json"
            external.write_text('{"pid": 999999, "created_at": "outside"}\n', encoding="utf-8")
            before = (external.read_bytes(), sha256(external))
            (state / "local-core-migration.lock").symlink_to(external)

            with self.assertRaisesRegex(
                Exception,
                "unsafe-project-path: .ai-team/state/local-core-migration.lock",
            ):
                with SqliteStore(root).connection():
                    self.fail("SQLite opened through an unsafe sentinel authority")

            self.assertEqual((external.read_bytes(), sha256(external)), before)

    def test_store_rejects_unsafe_database_sidecars_before_sqlite_open(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                root = base / "project"
                harness_db.init_runtime(root)
                external = base / f"outside{suffix}"
                external.write_bytes(b"outside-sidecar\n")
                before = external_identity(external)
                sidecar = Path(str(root / ".ai-team/state/harness.db") + suffix)
                sidecar.unlink(missing_ok=True)
                sidecar.symlink_to(external)

                with self.assertRaisesRegex(
                    Exception,
                    rf"unsafe-project-path: \.ai-team/state/harness\.db{suffix}",
                ):
                    with SqliteStore(root).connection() as conn:
                        conn.execute("select count(*) from project").fetchone()

                self.assertEqual(external_identity(external), before)

    def test_store_rejects_hard_linked_database_sidecars_before_sqlite_open(self) -> None:
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                root = base / "project"
                harness_db.init_runtime(root)
                external = base / f"outside-hardlink{suffix}"
                external.write_bytes(b"outside-hardlinked-sidecar\n")
                before = external_identity(external)
                sidecar = Path(str(root / ".ai-team/state/harness.db") + suffix)
                sidecar.unlink(missing_ok=True)
                os.link(external, sidecar)

                with self.assertRaisesRegex(
                    Exception,
                    rf"unsafe-project-path: \.ai-team/state/harness\.db{suffix}: hard-linked-target",
                ):
                    with SqliteStore(root).connection() as conn:
                        conn.execute("select count(*) from project").fetchone()

                self.assertEqual(external_identity(external), before)

    def test_store_backup_rejects_symlinked_destination(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            harness_db.init_runtime(root)
            external = base / "outside-backup.db"
            external.write_bytes(b"outside-backup\n")
            before = external_identity(external)
            target = root / ".ai-team/backups/manual.db"
            target.parent.mkdir(parents=True)
            target.symlink_to(external)

            with self.assertRaisesRegex(
                Exception,
                "unsafe-project-path: .ai-team/backups/manual.db",
            ):
                SqliteStore(root).backup_to(target)

            self.assertEqual(external_identity(external), before)

    def test_in_memory_store_remains_independent_of_project_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sentinel = root / ".ai-team/state/local-core-migration.lock"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_bytes(b"unsafe-for-file-store\n")
            store = InMemoryStore(root)
            self.addCleanup(store.close)
            with store.connection() as conn:
                conn.execute("create table sample(value text not null)")
                conn.commit()
            with store.transaction() as conn:
                conn.execute("insert into sample values ('ok')")
            with store.connection() as conn:
                self.assertEqual(conn.execute("select value from sample").fetchone()[0], "ok")

    def test_init_preflights_every_projection_and_retired_view_before_first_write(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        for relative in PROJECTION_ROLLBACK_PATHS:
            with self.subTest(relative=relative.as_posix()), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                root = base / "project"
                target = root / relative
                target.parent.mkdir(parents=True)
                external = base / f"outside-{relative.name}"
                external.write_bytes(b"outside-projection-authority\n")
                external.chmod(0o640)
                before = external_identity(external)
                target.symlink_to(external)

                with self.assertRaisesRegex(
                    Exception,
                    rf"unsafe-project-path: {relative.as_posix()}",
                ):
                    harness_db.init_runtime(root)

                self.assertEqual(external_identity(external), before)
                self.assertFalse((root / ".ai-team/state/harness.db").exists())

    def test_init_preflights_all_agent_template_destinations_before_first_write(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        for name in sorted(harness_db.CODEX_AGENT_TEMPLATE_NAMES):
            relative = Path(".codex/agents") / name
            with self.subTest(relative=relative.as_posix()), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                root = base / "project"
                target = root / relative
                target.parent.mkdir(parents=True)
                external = base / f"outside-{name}"
                external.write_bytes(b"outside-agent-authority\n")
                before = external_identity(external)
                target.symlink_to(external)

                with self.assertRaisesRegex(
                    Exception,
                    rf"unsafe-project-path: {relative.as_posix()}",
                ):
                    harness_db.init_runtime(root)

                self.assertEqual(external_identity(external), before)
                self.assertFalse((root / ".ai-team/state/harness.db").exists())

    def test_mutation_preflights_full_projection_inventory_before_commit(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            harness_db.init_runtime(root)
            unsafe_relative = PROJECTION_PATHS[1]
            unsafe = root / unsafe_relative
            unsafe.unlink()
            external = base / "outside-unselected-projection.md"
            external.write_bytes(b"outside-unselected-projection\n")
            before = external_identity(external)
            unsafe.symlink_to(external)

            with self.assertRaisesRegex(
                Exception,
                rf"unsafe-project-path: {unsafe_relative.as_posix()}",
            ):
                harness_db.record_decision(
                    root,
                    "must not commit",
                    "another projection is unsafe",
                )

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                self.assertEqual(
                    conn.execute(
                        "select count(*) from decisions where decision='must not commit'"
                    ).fetchone()[0],
                    0,
                )
            self.assertEqual(external_identity(external), before)

    def test_projection_verifier_rejects_linked_live_view_without_reading_referent(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        from core.projections import projection_content_issues

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            harness_db.init_runtime(root)
            relative = PROJECTION_PATHS[0]
            target = root / relative
            target.unlink()
            external = base / "outside-verifier.yaml"
            external.write_bytes(b"schema_version: 30\noutside: true\n")
            before = external_identity(external)
            target.symlink_to(external)

            issues = projection_content_issues(root)

            self.assertTrue(
                any(f"missing or unsafe view: {relative.as_posix()}" in issue for issue in issues),
                issues,
            )
            self.assertEqual(external_identity(external), before)

    def test_local_executor_rejects_linked_stdout_destination(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            root.mkdir()
            execution_id = "fixed-local-execution"
            artifact = root / ".ai-team/runtime/executions" / execution_id / "stdout.txt"
            artifact.parent.mkdir(parents=True)
            external = base / "outside-stdout.txt"
            external.write_bytes(b"outside-stdout\n")
            before = external_identity(external)
            artifact.symlink_to(external)

            with patch("core.execution.uuid.uuid4") as mocked_uuid:
                mocked_uuid.return_value.hex = execution_id
                with self.assertRaisesRegex(
                    Exception,
                    rf"unsafe-project-path: \.ai-team/runtime/executions/{execution_id}/stdout.txt",
                ):
                    LocalExecutor(root).run(
                        "python3 -c \"print('Ran 1 test')\"",
                        allowed_prefixes=["python3"],
                    )

            self.assertEqual(external_identity(external), before)

    def test_local_executor_rejects_linked_structured_result_source(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            root.mkdir()
            external = base / "outside-result.json"
            external.write_text(
                '{"summary":{"total":1,"passed":1,"failed":0,"errors":0}}',
                encoding="utf-8",
            )
            before = external_identity(external)
            (root / "result.json").symlink_to(external)

            with self.assertRaisesRegex(
                Exception,
                "unsafe-project-path: result.json",
            ):
                LocalExecutor(root).run(
                    "python3 -c \"print('done')\"",
                    allowed_prefixes=["python3"],
                    result_format="pytest-json",
                    result_path="result.json",
                )

            self.assertEqual(external_identity(external), before)

    def test_container_executor_rejects_linked_artifact_destination_before_run(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            root.mkdir()
            execution_id = "fixed-container-execution"
            artifact = root / ".ai-team/runtime/executions" / execution_id / "stdout.txt"
            artifact.parent.mkdir(parents=True)
            external = base / "outside-container.txt"
            external.write_bytes(b"outside-container\n")
            before = external_identity(external)
            artifact.symlink_to(external)

            with (
                patch("core.execution.uuid.uuid4") as mocked_uuid,
                patch("core.execution.shutil.which", return_value="/usr/bin/docker"),
                patch("core.execution.subprocess.run") as mocked_run,
            ):
                mocked_uuid.return_value.hex = execution_id
                with self.assertRaisesRegex(
                    Exception,
                    rf"unsafe-project-path: \.ai-team/runtime/executions/{execution_id}/stdout.txt",
                ):
                    ContainerExecutor(root).run(
                        "python3 -c \"print('Ran 1 test')\"",
                        target_id="UNIT",
                        target_command_template="python3 -c \"print('Ran 1 test')\"",
                    )
                mocked_run.assert_not_called()

            self.assertEqual(external_identity(external), before)

    def test_verify_rejects_linked_stdout_without_persisting_facts(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_candidate(root)
            initialize_target(root)
            execution_id = "linked-verify-stdout"
            artifact = root / ".ai-team/runtime/executions" / execution_id / "stdout.txt"
            artifact.parent.mkdir(parents=True)
            victim = root / ".ai-team/runtime/linked-victim.txt"
            victim.parent.mkdir(parents=True, exist_ok=True)
            victim.write_bytes(b"must-stay-unchanged\n")
            before = external_identity(victim)
            artifact.symlink_to(victim)
            fake_uuid = types.SimpleNamespace(
                uuid4=lambda: types.SimpleNamespace(hex=execution_id)
            )

            with patch("core.execution.uuid", fake_uuid):
                with self.assertRaisesRegex(
                    Exception,
                    rf"unsafe-project-path: \.ai-team/runtime/executions/{execution_id}/stdout.txt",
                ):
                    harness_db.verify_run(root, "UNIT", acceptance="AC1")

            self.assertEqual(external_identity(victim), before)
            self.assertEqual(execution_fact_counts(root), (0, 0, 0))

    def test_verify_rejects_linked_structured_source_and_destination(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        for attack in ("source", "destination"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                create_candidate(root)
                command = "python3 -B -m unittest test_candidate.py"
                result_path = ".ai-team/runtime/input/result.json"
                for args in (
                    ("init",),
                    ("acceptance", "add", "--id", "AC1", "--criterion", "structured passes"),
                    (
                        "test-target",
                        "add",
                        "--id",
                        "STRUCTURED",
                        "--kind",
                        "unit",
                        "--command-template",
                        command,
                        "--result-format",
                        "pytest-json",
                        "--result-path",
                        result_path,
                    ),
                ):
                    result = run_harness(root, *args)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

                execution_id = f"linked-structured-{attack}"
                artifact_dir = root / ".ai-team/runtime/executions" / execution_id
                artifact_dir.mkdir(parents=True)
                victim = root / ".ai-team/runtime" / f"structured-{attack}-victim.json"
                victim.parent.mkdir(parents=True, exist_ok=True)
                victim.write_text(
                    '{"summary":{"total":1,"passed":1,"failed":0,"errors":0}}',
                    encoding="utf-8",
                )
                before = external_identity(victim)
                if attack == "source":
                    source = root / result_path
                    source.parent.mkdir(parents=True, exist_ok=True)
                    source.symlink_to(victim)
                else:
                    source = root / result_path
                    source.parent.mkdir(parents=True, exist_ok=True)
                    source.write_bytes(victim.read_bytes())
                    (artifact_dir / "structured-result").symlink_to(victim)
                fake_uuid = types.SimpleNamespace(
                    uuid4=lambda: types.SimpleNamespace(hex=execution_id)
                )

                with patch("core.execution.uuid", fake_uuid):
                    with self.assertRaisesRegex(Exception, "unsafe-project-path"):
                        harness_db.verify_run(
                            root,
                            "STRUCTURED",
                            acceptance="AC1",
                        )

                self.assertEqual(external_identity(victim), before)
                self.assertEqual(execution_fact_counts(root), (0, 0, 0))

    def test_container_verify_rejects_linked_stdout_without_persisting_facts(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_candidate(root)
            initialize_target(root)
            execution_id = "linked-container-verify"
            artifact = root / ".ai-team/runtime/executions" / execution_id / "stdout.txt"
            artifact.parent.mkdir(parents=True)
            victim = root / ".ai-team/runtime/container-victim.txt"
            victim.parent.mkdir(parents=True, exist_ok=True)
            victim.write_bytes(b"must-stay-container\n")
            before = external_identity(victim)
            artifact.symlink_to(victim)
            fake_uuid = types.SimpleNamespace(
                uuid4=lambda: types.SimpleNamespace(hex=execution_id)
            )

            original_subprocess_run = subprocess.run

            def fake_container_run(argv, **kwargs):
                mounts = [value for value in argv if str(value).endswith(":/artifacts:rw")]
                if not mounts:
                    return original_subprocess_run(argv, **kwargs)
                mount = mounts[0]
                artifact_dir = Path(mount.removesuffix(":/artifacts:rw"))
                (artifact_dir / "stdout.txt").write_text(
                    "Ran 1 test in 0.001s\nOK\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(argv, 0, "", "")

            with (
                patch("core.execution.uuid", fake_uuid),
                patch("core.execution.shutil.which", return_value="/usr/bin/docker"),
                patch("core.execution.subprocess.run", side_effect=fake_container_run),
            ):
                with self.assertRaisesRegex(Exception, "unsafe-project-path"):
                    harness_db.verify_run(
                        root,
                        "UNIT",
                        acceptance="AC1",
                        runner="container",
                    )

            self.assertEqual(external_identity(victim), before)
            self.assertEqual(execution_fact_counts(root), (0, 0, 0))

    def test_second_validation_rejects_artifact_exchange_without_facts(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_candidate(root)
            initialize_target(root)
            first_validation_complete = threading.Event()
            exchange_complete = threading.Event()
            artifact_ready: list[Path] = []
            original_validate = execution_module.validate_execution_result
            calls = 0

            def validate_then_pause(*args, **kwargs):
                nonlocal calls
                original_validate(*args, **kwargs)
                calls += 1
                if calls == 1:
                    result = args[2]
                    artifact_ready.append(root / result.artifact_path)
                    first_validation_complete.set()
                    if not exchange_complete.wait(5):
                        raise AssertionError("artifact exchange did not complete")

            def exchange_artifact() -> None:
                if not first_validation_complete.wait(5):
                    return
                artifact = artifact_ready[0]
                victim = root / ".ai-team/runtime/same-content-artifact.txt"
                victim.parent.mkdir(parents=True, exist_ok=True)
                victim.write_bytes(artifact.read_bytes())
                artifact.unlink()
                artifact.symlink_to(victim)
                exchange_complete.set()

            attacker = threading.Thread(target=exchange_artifact)
            attacker.start()
            self.addCleanup(attacker.join, 5)
            with patch.object(
                execution_module,
                "validate_execution_result",
                side_effect=validate_then_pause,
            ):
                with self.assertRaisesRegex(Exception, "unsafe-project-path"):
                    harness_db.verify_run(root, "UNIT", acceptance="AC1")
            attacker.join(5)
            self.assertFalse(attacker.is_alive())
            self.assertEqual(execution_fact_counts(root), (0, 0, 0))

    def test_migration_rejects_symlinked_backup_root_before_copy(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            init_schema29_fixture(root)
            external = base / "outside-backups"
            external.mkdir()
            before = (external.stat().st_ino, tuple(external.iterdir()))
            backups = root / ".ai-team/backups"
            backups.symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(
                Exception,
                "unsafe-project-path: .ai-team/backups: unsafe-ancestor",
            ):
                local_core_migration.migrate_project_to_schema30(
                    root,
                    active_validator=lambda _active: None,
                )

            self.assertEqual((external.stat().st_ino, tuple(external.iterdir())), before)

    def test_migration_rejects_linked_sentinel_without_touching_referent(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            init_schema29_fixture(root)
            external = base / "outside-migration.lock"
            external.write_bytes(b"outside-sentinel\n")
            before = external_identity(external)
            sentinel = root / ".ai-team/state/local-core-migration.lock"
            sentinel.symlink_to(external)

            with self.assertRaisesRegex(
                Exception,
                "unsafe-project-path: .ai-team/state/local-core-migration.lock",
            ):
                local_core_migration.migrate_project_to_schema30(
                    root,
                    active_validator=lambda _active: None,
                )

            self.assertEqual(external_identity(external), before)

    def test_migration_rejects_linked_staging_database_before_conversion(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            init_schema29_fixture(root)
            active = root / ".ai-team/state/harness.db"
            staging = root / ".ai-team/backups/manual/harness.schema30.new.db"
            staging.parent.mkdir(parents=True)
            external = base / "outside-staging.db"
            external.write_bytes(active.read_bytes())
            before = external_identity(external)
            staging.symlink_to(external)

            with self.assertRaisesRegex(
                Exception,
                "unsafe-project-path: .ai-team/backups/manual/harness.schema30.new.db",
            ):
                local_core_migration.stage_supported_schema_to_schema30(
                    active,
                    staging,
                    project_root=root,
                )

            self.assertEqual(external_identity(external), before)

    def test_migration_manifest_atomic_write_rejects_linked_target(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            target = root / ".ai-team/backups/manual/migration-manifest.json"
            target.parent.mkdir(parents=True)
            external = base / "outside-manifest.json"
            external.write_bytes(b'{"outside":true}\n')
            before = external_identity(external)
            target.symlink_to(external)

            with self.assertRaisesRegex(
                Exception,
                "unsafe-project-path: .ai-team/backups/manual/migration-manifest.json",
            ):
                local_core_migration._write_json_atomic(target, {"status": "unsafe"})

            self.assertEqual(external_identity(external), before)

    def test_projection_backup_rejects_linked_backup_directory(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            init_schema29_fixture(root)
            backup_dir = root / ".ai-team/backups/manual"
            backup_dir.mkdir(parents=True)
            external = base / "outside-projection-backup"
            external.mkdir()
            before = (external.stat().st_ino, tuple(external.iterdir()))
            (backup_dir / "projections").symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(
                Exception,
                "unsafe-project-path: .ai-team/backups/manual/projections",
            ):
                local_core_migration._create_projection_backup(root, backup_dir)

            self.assertEqual((external.stat().st_ino, tuple(external.iterdir())), before)

    def test_projection_restore_rejects_linked_live_target(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            init_schema29_fixture(root)
            target = root / PROJECTION_PATHS[0]
            target.parent.mkdir(parents=True)
            target.write_bytes(b"schema_version: 29\n")
            backup_dir = root / ".ai-team/backups/manual"
            backup_dir.mkdir(parents=True)
            projection_backup = local_core_migration._create_projection_backup(root, backup_dir)
            target.unlink()
            external = base / "outside-live-projection.yaml"
            external.write_bytes(b"outside-live-projection\n")
            before = external_identity(external)
            target.symlink_to(external)

            with self.assertRaisesRegex(
                Exception,
                rf"unsafe-project-path: {PROJECTION_PATHS[0].as_posix()}",
            ):
                local_core_migration._restore_projection_backup(root, projection_backup)

            self.assertEqual(external_identity(external), before)

    def test_failed_database_preservation_rejects_linked_destination(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            init_schema29_fixture(root)
            active = root / ".ai-team/state/harness.db"
            active_before = sha256(active)
            failed = root / ".ai-team/backups/manual/harness.schema30.failed-after-activation.db"
            failed.parent.mkdir(parents=True)
            external = base / "outside-failed.db"
            external.write_bytes(b"outside-failed\n")
            before = external_identity(external)
            failed.symlink_to(external)

            with self.assertRaisesRegex(
                Exception,
                "unsafe-project-path: .ai-team/backups/manual/harness.schema30.failed-after-activation.db",
            ):
                local_core_migration._preserve_failed_schema30(active, failed)

            self.assertTrue(active.is_file())
            self.assertEqual(sha256(active), active_before)
            self.assertEqual(external_identity(external), before)

    def test_failed_database_sidecar_rejects_linked_quarantine_target(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            active = root / ".ai-team/state/harness.db"
            active.parent.mkdir(parents=True)
            active.write_bytes(b"db")
            active_wal = Path(str(active) + "-wal")
            active_wal.write_bytes(b"wal")
            failed = root / ".ai-team/backups/manual/failed.db"
            failed.parent.mkdir(parents=True)
            external = base / "outside-failed-wal"
            external.write_bytes(b"outside-wal\n")
            before = external_identity(external)
            Path(str(failed) + "-wal").symlink_to(external)

            with self.assertRaisesRegex(
                Exception,
                "unsafe-project-path: .ai-team/backups/manual/failed.db-wal",
            ):
                local_core_migration._quarantine_failed_database_sidecars(active, failed)

            self.assertEqual(active_wal.read_bytes(), b"wal")
            self.assertEqual(external_identity(external), before)

    def test_database_restore_rejects_linked_temporary_destination(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            init_schema29_fixture(root)
            active = root / ".ai-team/state/harness.db"
            backup = schema_lifecycle.backup_sqlite_database(
                root,
                source_path=active,
                expected_source_version=29,
                created_at="2026-07-15T00:00:00Z",
            )
            restore = active.with_name(active.name + ".restore")
            external = base / "outside-restore.db"
            external.write_bytes(b"outside-restore\n")
            before = external_identity(external)
            restore.symlink_to(external)

            with self.assertRaisesRegex(
                Exception,
                "unsafe-project-path: .ai-team/state/harness.db.restore",
            ):
                local_core_migration._restore_verified_backup(active, backup)

            self.assertEqual(external_identity(external), before)

    def test_migration_preactivation_manifest_temp_link_preserves_schema29(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            init_schema29_fixture(root)
            active = root / ".ai-team/state/harness.db"
            source_digest = sha256(active)
            external = base / "outside-manifest-temp.json"
            external.write_bytes(b'{"outside":true}\n')
            before = external_identity(external)
            original_backup = local_core_migration.backup_sqlite_database

            def backup_then_attack(*args, **kwargs):
                backup = original_backup(*args, **kwargs)
                manifest_temp = Path(backup.backup_path).parent / "migration-manifest.json.tmp"
                manifest_temp.symlink_to(external)
                return backup

            caught: BaseException | None = None
            with patch.object(
                local_core_migration,
                "backup_sqlite_database",
                side_effect=backup_then_attack,
            ):
                try:
                    local_core_migration.migrate_project_to_schema30(
                        root,
                        active_validator=_active_projection_validator(root),
                    )
                except BaseException as exc:
                    caught = exc

            self.assertIsNotNone(caught)
            self.assertIn("unsafe-project-path", str(caught))
            self.assertEqual(external_identity(external), before)
            self.assertFalse(active.is_symlink())
            self.assertEqual(sha256(active), source_digest)

    def test_migration_postactivation_restore_link_stays_recovery_required(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            init_schema29_fixture(root)
            active = root / ".ai-team/state/harness.db"
            external = base / "outside-restore-after-activation.db"
            external.write_bytes(b"outside-restore-after-activation\n")
            before = external_identity(external)

            def attack_after_activation(_active: Path) -> None:
                active.with_name(active.name + ".restore").symlink_to(external)
                raise RuntimeError("injected activation failure")

            caught: BaseException | None = None
            try:
                local_core_migration.migrate_project_to_schema30(
                    root,
                    active_validator=attack_after_activation,
                )
            except BaseException as exc:
                caught = exc

            self.assertIsNotNone(caught)
            self.assertIn("unsafe-project-path", str(caught))
            backup_dir = next(
                (root / ".ai-team/backups").glob("schema-29-before-local-core-*")
            )
            manifest = __import__("json").loads(
                (backup_dir / "migration-manifest.json").read_text(encoding="utf-8")
            )
            sentinel = root / ".ai-team/state/local-core-migration.lock"
            self.assertEqual(manifest["status"], "rollback-incomplete")
            self.assertIn("injected activation failure", manifest["error"])
            self.assertIn("unsafe-project-path", manifest["database_restore_error"])
            self.assertTrue(sentinel.is_file())
            self.assertEqual(external_identity(external), before)
            self.assertFalse(active.is_symlink())


class ProjectFSContractRedTests(unittest.TestCase):
    def test_relative_path_grammar_rejects_escape_and_windows_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fs = project_fs(Path(temp))
            invalid = (
                Path("../outside"),
                Path("/absolute"),
                Path("C:/outside"),
                Path("//server/share"),
                Path("safe/name:stream"),
                Path("CON"),
                Path("trailing. "),
            )
            for relative in invalid:
                with self.subTest(relative=str(relative)):
                    with self.assertRaisesRegex(Exception, "invalid-relative-path"):
                        fs.audit((relative,))

    def test_root_symlink_alias_is_resolved_once_but_descendant_link_is_rejected(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX root alias case; Windows junction case is separate")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            real_root = base / "real"
            real_root.mkdir()
            alias = base / "alias"
            alias.symlink_to(real_root, target_is_directory=True)
            external = base / "outside.txt"
            external.write_bytes(b"outside")
            (real_root / "linked.txt").symlink_to(external)

            fs = project_fs(alias)
            fs.atomic_write(Path("safe.txt"), b"safe\n")
            self.assertEqual((real_root / "safe.txt").read_bytes(), b"safe\n")
            with self.assertRaisesRegex(Exception, "unsafe-target"):
                fs.read_bytes(Path("linked.txt"))

    def test_fifo_and_socket_targets_are_rejected_without_opening(self) -> None:
        if os.name == "nt" or not hasattr(os, "mkfifo"):
            self.skipTest("POSIX non-regular path primitives unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fifo = root / "fifo"
            os.mkfifo(fifo)
            sock_path = root / "socket"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(server.close)
            server.bind(os.fspath(sock_path))
            fs = project_fs(root)

            for relative in (Path("fifo"), Path("socket")):
                with self.subTest(relative=str(relative)):
                    with self.assertRaisesRegex(Exception, "unsafe-target"):
                        fs.audit((relative,), allow_missing=False)

    def test_atomic_write_detects_target_exchange_before_publish(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target.txt"
            target.write_bytes(b"before\n")
            external = root.parent / f"{root.name}-outside.txt"
            external.write_bytes(b"outside\n")
            self.addCleanup(external.unlink, missing_ok=True)
            original_hook = project_fs_module._before_atomic_replace
            replace_window = threading.Event()
            exchange_complete = threading.Event()

            def pause_before_replace(_fs, relative: Path) -> None:
                if relative == Path("target.txt"):
                    replace_window.set()
                    if not exchange_complete.wait(5):
                        raise AssertionError("attacker exchange did not complete")

            def exchange_target() -> None:
                if not replace_window.wait(5):
                    return
                target.unlink()
                target.symlink_to(external)
                exchange_complete.set()

            attacker = threading.Thread(target=exchange_target)
            attacker.start()
            self.addCleanup(attacker.join, 5)
            project_fs_module._before_atomic_replace = pause_before_replace
            self.addCleanup(setattr, project_fs_module, "_before_atomic_replace", original_hook)
            with self.assertRaisesRegex(Exception, "path-identity-changed"):
                project_fs(root).atomic_write(Path("target.txt"), b"after\n")
            attacker.join(5)
            self.assertFalse(attacker.is_alive())

            self.assertEqual(external.read_bytes(), b"outside\n")

    def test_local_execution_detects_stdout_exchange_before_publish(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            root.mkdir()
            execution_id = "raced-local-execution"
            artifact = root / ".ai-team/runtime/executions" / execution_id / "stdout.txt"
            external = base / "outside-raced-stdout.txt"
            external.write_bytes(b"outside-raced-stdout\n")
            before = external_identity(external)
            replace_window = threading.Event()
            exchange_complete = threading.Event()
            original_hook = project_fs_module._before_atomic_replace

            def pause_before_replace(_fs, relative: Path) -> None:
                expected = Path(f".ai-team/runtime/executions/{execution_id}/stdout.txt")
                if relative == expected:
                    replace_window.set()
                    if not exchange_complete.wait(5):
                        raise AssertionError("execution attacker exchange did not complete")

            def exchange_target() -> None:
                if not replace_window.wait(5):
                    return
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.unlink(missing_ok=True)
                artifact.symlink_to(external)
                exchange_complete.set()

            attacker = threading.Thread(target=exchange_target)
            attacker.start()
            self.addCleanup(attacker.join, 5)
            project_fs_module._before_atomic_replace = pause_before_replace
            self.addCleanup(setattr, project_fs_module, "_before_atomic_replace", original_hook)
            with patch("core.execution.uuid.uuid4") as mocked_uuid:
                mocked_uuid.return_value.hex = execution_id
                with self.assertRaisesRegex(Exception, "path-identity-changed"):
                    LocalExecutor(root).run(
                        "python3 -c \"print('Ran 1 test')\"",
                        allowed_prefixes=["python3"],
                    )
            attacker.join(5)
            self.assertFalse(attacker.is_alive())
            self.assertEqual(external_identity(external), before)

    @unittest.skipUnless(os.name == "nt", "Windows junction contract")
    def test_windows_junction_ancestor_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            root.mkdir()
            outside = base / "outside"
            outside.mkdir()
            junction = root / "junction"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                text=True,
                capture_output=True,
                check=False,
            )
            if created.returncode != 0:
                self.skipTest(f"junction primitive unavailable: {created.stderr.strip()}")
            with self.assertRaisesRegex(Exception, "unsafe-ancestor"):
                project_fs(root).atomic_write(Path("junction/result.txt"), b"unsafe")
            self.assertFalse((outside / "result.txt").exists())

    @unittest.skipUnless(os.name == "nt", "Windows handle-publish contract")
    def test_windows_replace_pins_exact_source_handle_through_publish(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "staging.db"
            destination = root / "active.db"
            parked = root / "parked.db"
            source.write_bytes(b"verified-staging\n")
            destination.write_bytes(b"previous-active\n")
            original_hook = project_fs_module._before_windows_handle_rename
            attacker_errors: list[BaseException] = []
            attacker_done = threading.Event()

            def attack_source_path() -> None:
                try:
                    os.replace(source, parked)
                    source.write_bytes(b"substituted-staging\n")
                except BaseException as exc:
                    attacker_errors.append(exc)
                finally:
                    attacker_done.set()

            def race_while_handle_is_pinned(_backend, relative: Path, _destination: Path) -> None:
                if relative != Path("staging.db"):
                    return
                attacker = threading.Thread(target=attack_source_path)
                attacker.start()
                if not attacker_done.wait(5):
                    raise AssertionError("Windows source-path attacker did not finish")
                attacker.join(5)
                if attacker.is_alive():
                    raise AssertionError("Windows source-path attacker remained alive")

            project_fs_module._before_windows_handle_rename = race_while_handle_is_pinned
            self.addCleanup(
                setattr,
                project_fs_module,
                "_before_windows_handle_rename",
                original_hook,
            )

            project_fs(root).replace_file(
                Path("staging.db"),
                Path("active.db"),
            )

            self.assertTrue(attacker_errors)
            self.assertIsInstance(attacker_errors[0], OSError)
            self.assertEqual(destination.read_bytes(), b"verified-staging\n")
            self.assertFalse(source.exists())
            self.assertFalse(parked.exists())

    @unittest.skipUnless(os.name == "nt", "Windows handle-publish contract")
    def test_windows_missing_target_race_fails_without_overwrite_or_temp(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "new-canonical.txt"
            original_hook = project_fs_module._before_windows_handle_rename

            def create_raced_target(
                _backend,
                _source: Path,
                destination: Path,
            ) -> None:
                if destination == Path("new-canonical.txt"):
                    target.write_bytes(b"raced-authority\n")

            project_fs_module._before_windows_handle_rename = create_raced_target
            self.addCleanup(
                setattr,
                project_fs_module,
                "_before_windows_handle_rename",
                original_hook,
            )

            with self.assertRaisesRegex(Exception, "path-identity-changed"):
                project_fs(root).atomic_write(
                    Path("new-canonical.txt"),
                    b"must-not-overwrite\n",
                )

            self.assertEqual(target.read_bytes(), b"raced-authority\n")
            self.assertEqual(
                tuple(root.glob(".new-canonical.txt.kafa-*.tmp")),
                (),
            )

    @unittest.skipUnless(os.name == "nt", "Windows handle-publish contract")
    def test_windows_temp_hardlink_race_fails_before_publication(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "canonical.txt"
            outside_alias = root.parent / f"{root.name}-temp-alias.txt"
            target.write_bytes(b"previous-canonical\n")
            original_hook = project_fs_module._before_windows_handle_rename

            def hardlink_owned_temp(
                _backend,
                source: Path,
                destination: Path,
            ) -> None:
                if destination == Path("canonical.txt"):
                    os.link(root / source, outside_alias)

            project_fs_module._before_windows_handle_rename = hardlink_owned_temp
            self.addCleanup(
                setattr,
                project_fs_module,
                "_before_windows_handle_rename",
                original_hook,
            )
            self.addCleanup(outside_alias.unlink, missing_ok=True)

            with self.assertRaisesRegex(Exception, "hard-linked-target"):
                project_fs(root).atomic_write(
                    Path("canonical.txt"),
                    b"must-not-publish\n",
                )

            self.assertEqual(target.read_bytes(), b"previous-canonical\n")
            self.assertEqual(
                tuple(root.glob(".canonical.txt.kafa-*.tmp")),
                (),
            )
            self.assertEqual(outside_alias.read_bytes(), b"must-not-publish\n")

    @unittest.skipUnless(os.name == "nt", "Windows handle-delete contract")
    def test_windows_unlink_hardlink_race_fails_before_delete(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            root.mkdir()
            target = root / "retired.txt"
            outside_alias = base / "outside-alias.txt"
            target.write_bytes(b"must-remain-after-race\n")
            original_hook = project_fs_module._before_windows_handle_delete

            def hardlink_before_delete(_backend, relative: Path) -> None:
                if relative == Path("retired.txt"):
                    os.link(target, outside_alias)

            project_fs_module._before_windows_handle_delete = hardlink_before_delete
            self.addCleanup(
                setattr,
                project_fs_module,
                "_before_windows_handle_delete",
                original_hook,
            )

            with self.assertRaisesRegex(Exception, "hard-linked-target"):
                project_fs(root).unlink_regular(Path("retired.txt"))

            self.assertEqual(target.read_bytes(), b"must-remain-after-race\n")
            self.assertEqual(outside_alias.read_bytes(), b"must-remain-after-race\n")

    @unittest.skipUnless(os.name == "nt", "Windows pinned-directory contract")
    def test_windows_directory_create_holds_parent_against_replacement(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            parent = root / "backups"
            parent.mkdir(parents=True)
            parked = base / "parked-backups"
            original_hook = project_fs_module._before_windows_directory_create
            attacker_errors: list[BaseException] = []

            def attempt_parent_replace() -> None:
                try:
                    os.replace(parent, parked)
                except BaseException as exc:
                    attacker_errors.append(exc)

            def race_parent(_backend, relative: Path) -> None:
                if relative.parent != Path("backups"):
                    return
                attacker = threading.Thread(target=attempt_parent_replace)
                attacker.start()
                attacker.join(5)
                if attacker.is_alive():
                    raise AssertionError("Windows parent attacker remained alive")

            project_fs_module._before_windows_directory_create = race_parent
            self.addCleanup(
                setattr,
                project_fs_module,
                "_before_windows_directory_create",
                original_hook,
            )

            created = project_fs(root).create_unique_directory(
                Path("backups"),
                "schema-",
            )

            self.assertTrue(attacker_errors)
            self.assertIsInstance(attacker_errors[0], OSError)
            self.assertTrue((root / created).is_dir())
            self.assertFalse(parked.exists())


class ProjectFSFoundationTests(unittest.TestCase):
    def test_safe_read_write_copy_lock_unique_directory_and_unlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with project_fs(root) as fs:
                fs.atomic_write(Path("state/source.txt"), b"source\n", mode=0o640)
                self.assertEqual(fs.read_bytes(Path("state/source.txt")), b"source\n")
                fs.copy_file(
                    Path("state/source.txt"),
                    Path("state/copy.txt"),
                    mode=0o600,
                )
                self.assertEqual(fs.read_bytes(Path("state/copy.txt")), b"source\n")
                fs.create_exclusive(Path("state/exclusive.txt"), b"exclusive\n")
                with self.assertRaises(FileExistsError):
                    fs.create_exclusive(Path("state/exclusive.txt"), b"replace\n")
                descriptor = fs.open_lock_fd(Path("state/operation.lock"))
                try:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                unique = fs.create_unique_directory(Path("backups"), "schema-")
                fs.audit_directory(unique, allow_missing=False)
                fs.create_directory_exclusive(Path("staging/exclusive-dir"))
                fs.atomic_write(Path("staging/source.db"), b"database\n")
                fs.replace_file(
                    Path("staging/source.db"),
                    Path("state/activated.db"),
                )
                self.assertEqual(
                    fs.read_bytes(Path("state/activated.db")),
                    b"database\n",
                )
                fs.remove_empty_directory(Path("staging/exclusive-dir"))
                fs.unlink_regular(Path("state/copy.txt"))
                fs.audit((Path("state/copy.txt"),), allow_missing=True)

            self.assertFalse((root / "state/copy.txt").exists())
            self.assertEqual((root / "state/source.txt").read_bytes(), b"source\n")
            self.assertEqual(stat.S_IMODE((root / "state/source.txt").stat().st_mode), 0o640)

    def test_bounded_audit_rejects_unbounded_inventory(self) -> None:
        from core.project_fs import MAX_AUDIT_PATHS

        with tempfile.TemporaryDirectory() as temp:
            fs = project_fs(Path(temp))
            with self.assertRaisesRegex(Exception, "invalid-relative-path"):
                fs.audit(
                    tuple(Path(f"path-{index}") for index in range(MAX_AUDIT_PATHS + 1))
                )

    def test_windows_capability_fake_fails_closed_without_mutation(self) -> None:
        from core.project_fs import _WindowsBackend

        class UnavailableWindowsApi:
            available = False

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            before = tuple(root.iterdir())
            with self.assertRaisesRegex(Exception, "platform-safety-unavailable"):
                _WindowsBackend(root, api=UnavailableWindowsApi())
            self.assertEqual(tuple(root.iterdir()), before)

    def test_windows_rename_error_mapping_distinguishes_capability_and_race(self) -> None:
        from core.project_fs import _windows_rename_error_reason

        for code in (1, 50, 87, 120, 124):
            with self.subTest(code=code):
                self.assertEqual(
                    _windows_rename_error_reason(OSError(code, "unsupported")),
                    "platform-safety-unavailable",
                )
        for code in (5, 32, 80, 183):
            with self.subTest(code=code):
                self.assertEqual(
                    _windows_rename_error_reason(OSError(code, "race")),
                    "path-identity-changed",
                )

    @unittest.skipUnless(os.name == "nt", "Windows partial-write cleanup contract")
    def test_windows_partial_write_preserves_primary_error_and_cleans_target(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            unrelated = root / "unrelated.txt"
            unrelated.write_bytes(b"must-remain\n")
            fs = project_fs(root)
            backend = fs._backend
            original_hook = project_fs_module._after_windows_write_chunk
            original_delete = backend._delete_on_close
            delete_calls = 0

            def interrupt_after_first_chunk(
                _backend,
                relative: Path,
                written_total: int,
            ) -> None:
                if relative == Path("partial.bin") and written_total > 0:
                    raise KeyboardInterrupt("injected-partial-write")

            def fail_first_delete(handle: int, relative: Path) -> None:
                nonlocal delete_calls
                delete_calls += 1
                if delete_calls == 1:
                    raise OSError("injected-delete-on-close-failure")
                original_delete(handle, relative)

            project_fs_module._after_windows_write_chunk = interrupt_after_first_chunk
            backend._delete_on_close = fail_first_delete
            self.addCleanup(
                setattr,
                project_fs_module,
                "_after_windows_write_chunk",
                original_hook,
            )
            self.addCleanup(setattr, backend, "_delete_on_close", original_delete)

            with self.assertRaisesRegex(
                KeyboardInterrupt,
                "injected-partial-write",
            ) as caught:
                fs.create_exclusive(
                    Path("partial.bin"),
                    b"x" * (2 * 1024 * 1024),
                )

            self.assertTrue(
                any(
                    "delete-on-close failed" in note
                    for note in getattr(caught.exception, "__notes__", ())
                )
            )
            self.assertFalse((root / "partial.bin").exists())
            self.assertEqual(unrelated.read_bytes(), b"must-remain\n")
            project_fs_module._after_windows_write_chunk = original_hook
            backend._delete_on_close = original_delete
            fs.create_exclusive(Path("partial.bin"), b"recreated\n")
            self.assertEqual((root / "partial.bin").read_bytes(), b"recreated\n")

    def test_atomic_write_baseexception_closes_and_removes_owned_temporary(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "state.txt"
            target.write_bytes(b"before\n")
            original_hook = project_fs_module._before_atomic_replace

            def interrupt(_fs, _relative: Path) -> None:
                raise KeyboardInterrupt("injected foundation interruption")

            project_fs_module._before_atomic_replace = interrupt
            self.addCleanup(setattr, project_fs_module, "_before_atomic_replace", original_hook)
            with self.assertRaisesRegex(KeyboardInterrupt, "foundation interruption"):
                project_fs(root).atomic_write(Path("state.txt"), b"after\n")

            self.assertEqual(target.read_bytes(), b"before\n")
            self.assertEqual(tuple(root.glob(".state.txt.kafa-*.tmp")), ())


if __name__ == "__main__":
    unittest.main()
