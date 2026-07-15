from __future__ import annotations

import hashlib
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
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
from core.execution import ContainerExecutor, LocalExecutor  # noqa: E402
from core.projections import PROJECTION_PATHS, PROJECTION_ROLLBACK_PATHS  # noqa: E402
from core.store import InMemoryStore, SqliteStore, project_db_operation  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
