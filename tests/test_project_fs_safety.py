from __future__ import annotations

import errno
import hashlib
import multiprocessing
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
from contextlib import closing, contextmanager
from pathlib import Path, PureWindowsPath
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
from core import delivery as delivery_module  # noqa: E402
from core import execution as execution_module  # noqa: E402
from core import store as store_module  # noqa: E402
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


_FAKE_DOCKER = str(
    (
        Path(tempfile.gettempdir())
        / ("docker.exe" if os.name == "nt" else "docker")
    ).absolute()
)
_FAKE_DOCKER_CONTEXT = "default"
_FAKE_DOCKER_ENDPOINT = (
    "npipe:////./pipe/docker_engine"
    if os.name == "nt"
    else "unix:///var/run/docker.sock"
)
_FAKE_DOCKER_IMAGE_DIGEST = f"sha256:{'a' * 64}"
_REAL_SUBPROCESS_RUN = subprocess.run


def fake_local_docker_subprocess(argv, *, run_handler=None, **kwargs):
    """Model the local Docker control plane without invoking a real binary."""

    args = [str(value) for value in argv]
    if not args or args[0] != _FAKE_DOCKER:
        return _REAL_SUBPROCESS_RUN(argv, **kwargs)
    if args == [_FAKE_DOCKER, "context", "show"]:
        return subprocess.CompletedProcess(argv, 0, f"{_FAKE_DOCKER_CONTEXT}\n", "")
    if args == [_FAKE_DOCKER, "context", "inspect", _FAKE_DOCKER_CONTEXT]:
        return subprocess.CompletedProcess(
            argv,
            0,
            '[{"Endpoints":{"docker":{"Host":"%s"}}}]'
            % _FAKE_DOCKER_ENDPOINT,
            "",
        )

    endpoint_prefix = [_FAKE_DOCKER, "--host", _FAKE_DOCKER_ENDPOINT]
    if args[:3] != endpoint_prefix:
        raise AssertionError(f"unexpected Docker command: {args!r}")
    command = args[3:]
    if command[:1] == ["version"]:
        return subprocess.CompletedProcess(argv, 0, "27.0.0\n", "")
    if command[:2] == ["image", "inspect"]:
        return subprocess.CompletedProcess(
            argv,
            0,
            f'[{{"Id":"{_FAKE_DOCKER_IMAGE_DIGEST}"}}]',
            "",
        )
    if command[:1] == ["run"]:
        if run_handler is None:
            raise AssertionError("unexpected Docker run command")
        return run_handler(argv, **kwargs)
    if command[:2] == ["rm", "-f"]:
        return subprocess.CompletedProcess(argv, 0, "", "")
    raise AssertionError(f"unexpected Docker command: {args!r}")


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


def operation_lock_replacement_contender(
    root_value: str,
    first_outcome: multiprocessing.queues.Queue,
    result: multiprocessing.queues.Queue,
) -> None:
    root = Path(root_value)
    original_try = store_module._try_os_lock
    reported = False

    def observed_try(descriptor: int) -> None:
        nonlocal reported
        try:
            original_try(descriptor)
        except OSError:
            if not reported:
                reported = True
                first_outcome.put("blocked")
            raise
        if not reported:
            reported = True
            first_outcome.put("acquired")

    try:
        with patch.object(store_module, "_try_os_lock", side_effect=observed_try):
            with project_db_operation(root):
                result.put("entered")
    except BaseException as exc:
        result.put(f"error: {type(exc).__name__}: {exc}")


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
            if os.name == "nt":
                with project_db_operation(root):
                    with self.assertRaises(OSError) as blocked:
                        root.rename(detached)
                self.assertIsInstance(blocked.exception, PermissionError)
                self.assertIn(
                    getattr(blocked.exception, "winerror", None),
                    {5, 32},
                )
                self.assertTrue(root.is_dir())
                self.assertFalse(detached.exists())
                return
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

    def test_store_post_connect_authority_failure_closes_connection(self) -> None:
        from core.project_fs import ProjectPathSafetyError

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            real_connect = sqlite3.connect
            real_verify = SqliteStore._verify_connection_authority
            opened: list[object] = []
            verify_calls = 0

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

            def tracked_connect(*args, **kwargs):
                tracked = TrackedConnection(real_connect(*args, **kwargs))
                opened.append(tracked)
                return tracked

            def fail_first_authority_check(*args, **kwargs):
                nonlocal verify_calls
                verify_calls += 1
                if verify_calls == 1:
                    raise ProjectPathSafetyError(
                        Path(".ai-team/state/harness.db"),
                        "path-identity-changed",
                    )
                return real_verify(*args, **kwargs)

            try:
                with (
                    patch("core.store.sqlite3.connect", side_effect=tracked_connect),
                    patch.object(
                        SqliteStore,
                        "_verify_connection_authority",
                        side_effect=fail_first_authority_check,
                    ),
                ):
                    with self.assertRaisesRegex(
                        ProjectPathSafetyError,
                        "path-identity-changed",
                    ):
                        with SqliteStore(root).connection():
                            self.fail("unsafe SQLite connection was yielded")

                self.assertEqual(len(opened), 1)
                tracked = opened[0]
                self.assertTrue(tracked.closed)
                self.assertFalse(
                    any("journal_mode" in command for command in tracked.commands)
                )
                self.assertEqual(verify_calls, 3)
            finally:
                for tracked in opened:
                    tracked.close()

    def test_store_setup_failures_preserve_close_errors_and_do_not_retry(self) -> None:
        cases = (
            ("busy_timeout", RuntimeError("busy setup boom")),
            ("journal_mode", sqlite3.OperationalError("database is locked")),
            ("foreign_keys", RuntimeError("foreign key setup boom")),
        )
        for fragment, setup_error in cases:
            with self.subTest(fragment=fragment), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                harness_db.init_runtime(root)
                real_connect = sqlite3.connect
                connect_calls = 0

                class SetupFailingConnection:
                    def __init__(self, delegate) -> None:
                        object.__setattr__(self, "delegate", delegate)

                    def __getattr__(self, name):
                        return getattr(self.delegate, name)

                    def __setattr__(self, name, value) -> None:
                        if name == "delegate":
                            object.__setattr__(self, name, value)
                        else:
                            setattr(self.delegate, name, value)

                    def execute(self, sql, *args, **kwargs):
                        if fragment in str(sql):
                            raise setup_error
                        return self.delegate.execute(sql, *args, **kwargs)

                    def close(self) -> None:
                        self.delegate.close()
                        raise RuntimeError("close boom")

                def connect_with_setup_failure(*args, **kwargs):
                    nonlocal connect_calls
                    connect_calls += 1
                    return SetupFailingConnection(real_connect(*args, **kwargs))

                with patch(
                    "core.store.sqlite3.connect",
                    side_effect=connect_with_setup_failure,
                ):
                    with self.assertRaisesRegex(
                        type(setup_error),
                        str(setup_error),
                    ) as raised:
                        with SqliteStore(root).connection():
                            self.fail("setup-failed connection was yielded")

                notes = "\n".join(
                    getattr(raised.exception, "__notes__", ())
                )
                self.assertIn("SQLite connection close failed: close boom", notes)
                self.assertEqual(connect_calls, 1)

    def test_store_rejects_journal_created_after_wal_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            journal = root / ".ai-team/state/harness.db-journal"
            journal.unlink(missing_ok=True)
            real_connect = sqlite3.connect
            injected = False

            class InjectingConnection:
                def __init__(self, delegate) -> None:
                    object.__setattr__(self, "delegate", delegate)

                def __getattr__(self, name):
                    return getattr(self.delegate, name)

                def __setattr__(self, name, value) -> None:
                    if name == "delegate":
                        object.__setattr__(self, name, value)
                    else:
                        setattr(self.delegate, name, value)

                def execute(self, sql, *args, **kwargs):
                    nonlocal injected
                    result = self.delegate.execute(sql, *args, **kwargs)
                    if "journal_mode = wal" in str(sql) and not injected:
                        journal.write_bytes(b"injected-post-wal-journal\n")
                        injected = True
                    return result

            def connect_with_injection(*args, **kwargs):
                return InjectingConnection(real_connect(*args, **kwargs))

            with patch(
                "core.store.sqlite3.connect",
                side_effect=connect_with_injection,
            ):
                with self.assertRaisesRegex(
                    Exception,
                    r"harness\.db-journal: path-identity-changed",
                ):
                    with SqliteStore(root).connection():
                        self.fail("post-WAL journal injection was yielded")

            self.assertTrue(injected)
            self.assertEqual(
                journal.read_bytes(),
                b"injected-post-wal-journal\n",
            )

    def test_connection_rejects_source_sidecar_created_after_yield(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            journal = root / ".ai-team/state/harness.db-journal"
            journal.unlink(missing_ok=True)

            with self.assertRaisesRegex(
                Exception,
                r"harness\.db-journal: path-identity-changed",
            ):
                with SqliteStore(root).connection():
                    journal.write_bytes(b"injected-safe-journal\n")

            self.assertEqual(journal.read_bytes(), b"injected-safe-journal\n")

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

    @unittest.skipIf(os.name == "nt", "POSIX replaceable-inode contract")
    def test_operation_lock_inode_replacement_cannot_bypass_exclusion(self) -> None:
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first_outcome = context.Queue()
            result = context.Queue()
            contender = context.Process(
                target=operation_lock_replacement_contender,
                args=(str(root), first_outcome, result),
            )

            with self.assertRaisesRegex(Exception, "path-identity-changed"):
                with project_db_operation(root):
                    lock = root / ".ai-team/state/harness.db.operation.lock"
                    lock.unlink()
                    lock.write_bytes(b"\0")
                    lock.chmod(0o600)
                    contender.start()
                    self.assertEqual(first_outcome.get(timeout=5), "blocked")
                    self.assertTrue(contender.is_alive())

            self.assertEqual(result.get(timeout=5), "entered")
            contender.join(5)
            self.assertFalse(contender.is_alive())
            self.assertEqual(contender.exitcode, 0)

    def test_nested_project_fs_open_borrows_pinned_root_and_rejects_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            root.mkdir()
            detached = base / "detached-project"
            marker = b"replacement-root-must-remain-untouched\n"

            if os.name == "nt":
                with project_db_operation(root) as pinned_fs:
                    with self.assertRaises(OSError) as blocked:
                        root.rename(detached)
                    self.assertIsInstance(blocked.exception, PermissionError)
                    self.assertIn(
                        getattr(blocked.exception, "winerror", None),
                        {5, 32},
                    )
                    with project_fs(root) as nested_fs:
                        self.assertEqual(
                            nested_fs.root_identity_key,
                            pinned_fs.root_identity_key,
                        )
                        nested_fs.audit((Path(".gitignore"),), allow_missing=True)
                self.assertTrue(root.is_dir())
                self.assertFalse(detached.exists())
                return

            with self.assertRaisesRegex(
                Exception,
                "unsafe-project-path: .*path-identity-changed",
            ):
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

    def test_store_rechecks_sidecar_before_first_pragma(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            harness_db.init_runtime(root)
            sidecar = root / ".ai-team/state/harness.db-shm"
            sidecar.unlink(missing_ok=True)
            external = base / "outside-shm.bin"
            external.write_bytes(b"outside-shm-secret\n" * 4096)
            before = external_identity(external)
            real_connect = sqlite3.connect
            injected = False

            def inject_sidecar_then_connect(*args, **kwargs):
                nonlocal injected
                if not injected:
                    os.link(external, sidecar)
                    injected = True
                return real_connect(*args, **kwargs)

            with patch(
                "core.store.sqlite3.connect",
                side_effect=inject_sidecar_then_connect,
            ):
                with self.assertRaisesRegex(
                    Exception,
                    "unsafe-project-path: .ai-team/state/harness.db-shm: hard-linked-target",
                ):
                    with SqliteStore(root).connection():
                        self.fail("SQLite connection yielded after sidecar exchange")

            self.assertTrue(injected)
            self.assertEqual(external_identity(external), before)

    def test_backup_never_opens_final_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            harness_db.init_runtime(root)
            destination = base / "snapshot.db"
            external = base / "outside-secret.db"
            with closing(sqlite3.connect(destination)) as conn:
                conn.execute("create table previous(value text)")
                conn.execute("insert into previous values ('keep-until-publish')")
                conn.commit()
            with closing(sqlite3.connect(external)) as conn:
                conn.execute("create table secret(value text)")
                conn.execute("insert into secret values ('must-remain')")
                conn.commit()
            external_before = external_identity(external)
            real_connect = sqlite3.connect
            final_was_opened = False

            def redirect_if_final_is_opened(database, *args, **kwargs):
                nonlocal final_was_opened
                database_value = os.fspath(database)
                if (
                    database_value.split("?", 1)[0]
                    == destination.resolve(strict=True).as_uri()
                ):
                    final_was_opened = True
                    destination.unlink()
                    os.link(external, destination)
                return real_connect(database, *args, **kwargs)

            with patch(
                "core.store.sqlite3.connect",
                side_effect=redirect_if_final_is_opened,
            ):
                SqliteStore(root).backup_to(destination)

            self.assertFalse(final_was_opened)
            self.assertEqual(external_identity(external), external_before)
            with closing(sqlite3.connect(destination)) as conn:
                self.assertEqual(
                    conn.execute("select schema_version from project where id=1").fetchone()[0],
                    31,
                )

    def test_backup_failure_preserves_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            harness_db.init_runtime(root)
            destination = base / "snapshot.db"
            with closing(sqlite3.connect(destination)) as conn:
                conn.execute("create table sentinel(value text)")
                conn.execute("insert into sentinel values ('original')")
                conn.commit()
            before = external_identity(destination)
            store = SqliteStore(root)
            real_connect = store._connect

            class PartiallyFailingSource:
                def __init__(self, delegate) -> None:
                    self.delegate = delegate

                def __getattr__(self, name):
                    return getattr(self.delegate, name)

                def backup(self, target) -> None:
                    target.execute("drop table if exists sentinel")
                    target.execute("create table partial(value text)")
                    target.execute("insert into partial values ('incomplete')")
                    target.commit()
                    raise RuntimeError("injected backup failure")

            def connect_with_failing_backup(project_fs):
                source, database_identity, family_identity = real_connect(project_fs)
                return (
                    PartiallyFailingSource(source),
                    database_identity,
                    family_identity,
                )

            with patch.object(
                store,
                "_connect",
                side_effect=connect_with_failing_backup,
            ):
                with self.assertRaisesRegex(RuntimeError, "injected backup failure"):
                    store.backup_to(destination)

            self.assertEqual(external_identity(destination), before)

    def test_backup_rejects_existing_destination_sidecar_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            harness_db.init_runtime(root)
            destination = base / "snapshot.db"
            with closing(sqlite3.connect(destination)) as conn:
                conn.execute("create table sentinel(value text)")
                conn.execute("insert into sentinel values ('original')")
                conn.commit()
            sidecar = Path(str(destination) + "-wal")
            sidecar.write_bytes(b"preexisting-sidecar\n")
            destination_before = external_identity(destination)
            sidecar_before = external_identity(sidecar)

            with self.assertRaisesRegex(
                Exception,
                "project-db-backup-destination-busy",
            ):
                SqliteStore(root).backup_to(destination)

            self.assertEqual(external_identity(destination), destination_before)
            self.assertEqual(external_identity(sidecar), sidecar_before)
            self.assertEqual(
                tuple(base.glob(".snapshot.db.kafa-backup-*.tmp*")),
                (),
            )

    def test_backup_failure_preserves_missing_destination_and_cleans_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            harness_db.init_runtime(root)
            destination = base / "snapshot.db"
            store = SqliteStore(root)
            real_connect = store._connect

            class PartiallyFailingSource:
                def __init__(self, delegate) -> None:
                    self.delegate = delegate

                def __getattr__(self, name):
                    return getattr(self.delegate, name)

                def backup(self, target) -> None:
                    target.execute("create table partial(value text)")
                    target.execute("insert into partial values ('incomplete')")
                    target.commit()
                    raise RuntimeError("injected missing-destination backup failure")

            def connect_with_failing_backup(project_fs):
                source, database_identity, family_identity = real_connect(project_fs)
                return (
                    PartiallyFailingSource(source),
                    database_identity,
                    family_identity,
                )

            with patch.object(
                store,
                "_connect",
                side_effect=connect_with_failing_backup,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected missing-destination backup failure",
                ):
                    store.backup_to(destination)

            self.assertFalse(destination.exists())
            self.assertEqual(
                tuple(base.glob(".snapshot.db.kafa-backup-*.tmp*")),
                (),
            )

    def test_backup_rejects_final_identity_exchange_before_publication(self) -> None:
        from core.project_fs import ProjectFS

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            harness_db.init_runtime(root)
            destination = base / "snapshot.db"
            parked = base / "snapshot.original.db"
            replacement = base / "snapshot.replacement.db"
            destination.write_bytes(b"original-destination\n")
            replacement.write_bytes(b"raced-destination\n")
            replacement_before = external_identity(replacement)
            original_replace = ProjectFS.replace_file
            exchanged = False

            def exchange_then_replace(
                active_fs,
                source,
                target,
                *,
                expected_source=None,
                expected_destination=None,
            ) -> None:
                nonlocal exchanged
                if Path(target) == Path("snapshot.db") and not exchanged:
                    exchanged = True
                    destination.rename(parked)
                    replacement.rename(destination)
                original_replace(
                    active_fs,
                    source,
                    target,
                    expected_source=expected_source,
                    expected_destination=expected_destination,
                )

            with patch.object(
                ProjectFS,
                "replace_file",
                autospec=True,
                side_effect=exchange_then_replace,
            ):
                with self.assertRaisesRegex(
                    Exception,
                    "path-identity-changed",
                ):
                    SqliteStore(root).backup_to(destination)

            self.assertTrue(exchanged)
            self.assertEqual(
                external_identity(destination),
                replacement_before,
            )
            self.assertEqual(parked.read_bytes(), b"original-destination\n")
            self.assertEqual(
                tuple(base.glob(".snapshot.db.kafa-backup-*.tmp*")),
                (),
            )

    def test_backup_rejects_final_sidecar_injected_during_publication(self) -> None:
        from core.project_fs import ProjectFS

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            harness_db.init_runtime(root)
            destination = base / "snapshot.db"
            sidecar = Path(f"{destination}-wal")
            external = base / "outside-wal.bin"
            external.write_bytes(b"outside-wal-must-remain\n")
            external_before = external_identity(external)
            original_replace = ProjectFS.replace_file
            injected = False

            def inject_sidecar_then_replace(
                active_fs,
                source,
                target,
                *,
                expected_source=None,
                expected_destination=None,
            ) -> None:
                nonlocal injected
                if Path(target) == Path("snapshot.db") and not injected:
                    sidecar.symlink_to(external)
                    injected = True
                original_replace(
                    active_fs,
                    source,
                    target,
                    expected_source=expected_source,
                    expected_destination=expected_destination,
                )

            with patch.object(
                ProjectFS,
                "replace_file",
                autospec=True,
                side_effect=inject_sidecar_then_replace,
            ):
                with self.assertRaisesRegex(
                    Exception,
                    r"unsafe-project-path: snapshot\.db-wal",
                ):
                    SqliteStore(root).backup_to(destination)

            self.assertTrue(injected)
            self.assertEqual(external_identity(external), external_before)
            self.assertTrue(sidecar.is_symlink())

    def test_backup_rechecks_source_family_after_publication(self) -> None:
        from core.project_fs import ProjectFS

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            harness_db.init_runtime(root)
            destination = base / "snapshot.db"
            source_journal = root / ".ai-team/state/harness.db-journal"
            external = base / "outside-source-journal.bin"
            external.write_bytes(b"outside-source-journal\n")
            external_before = external_identity(external)
            original_replace = ProjectFS.replace_file
            injected = False

            def publish_then_inject_source_sidecar(
                active_fs,
                source,
                target,
                *,
                expected_source=None,
                expected_destination=None,
            ) -> None:
                nonlocal injected
                original_replace(
                    active_fs,
                    source,
                    target,
                    expected_source=expected_source,
                    expected_destination=expected_destination,
                )
                if Path(target) == Path("snapshot.db") and not injected:
                    os.link(external, source_journal)
                    injected = True

            with patch.object(
                ProjectFS,
                "replace_file",
                autospec=True,
                side_effect=publish_then_inject_source_sidecar,
            ):
                with self.assertRaisesRegex(
                    Exception,
                    r"unsafe-project-path: \.ai-team/state/harness\.db-journal: hard-linked-target",
                ):
                    SqliteStore(root).backup_to(destination)

            self.assertTrue(injected)
            self.assertEqual(external_identity(external), external_before)

    def test_backup_rejects_same_bytes_new_inode_after_final_assert(self) -> None:
        from core.project_fs import ProjectFS

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            harness_db.init_runtime(root)
            destination = base / "snapshot.db"
            parked = base / "snapshot.published.db"
            replacement = base / "snapshot.same-bytes.db"
            original_read = ProjectFS.read_bytes
            exchanged = False

            def exchange_then_read(
                active_fs,
                relative,
                *,
                max_bytes=None,
                expected=None,
            ):
                nonlocal exchanged
                if (
                    Path(relative) == Path("snapshot.db")
                    and expected is not None
                    and not exchanged
                ):
                    replacement.write_bytes(destination.read_bytes())
                    destination.rename(parked)
                    replacement.rename(destination)
                    exchanged = True
                return original_read(
                    active_fs,
                    relative,
                    max_bytes=max_bytes,
                    expected=expected,
                )

            with patch.object(
                ProjectFS,
                "read_bytes",
                autospec=True,
                side_effect=exchange_then_read,
            ):
                with self.assertRaisesRegex(Exception, "path-identity-changed"):
                    SqliteStore(root).backup_to(destination)

            self.assertTrue(exchanged)
            self.assertEqual(destination.read_bytes(), parked.read_bytes())

    def test_backup_preserves_primary_error_when_source_family_becomes_unsafe(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            harness_db.init_runtime(root)
            destination = base / "snapshot.db"
            source_journal = root / ".ai-team/state/harness.db-journal"
            external = base / "outside-failing-source-journal.bin"
            external.write_bytes(b"outside-failing-source-journal\n")
            store = SqliteStore(root)
            real_connect = store._connect

            class FailingSource:
                def __init__(self, delegate) -> None:
                    self.delegate = delegate

                def __getattr__(self, name):
                    return getattr(self.delegate, name)

                def backup(self, target) -> None:
                    os.link(external, source_journal)
                    raise RuntimeError("injected primary backup failure")

            def connect_with_failing_backup(project_fs):
                source, database_identity, family_identity = real_connect(project_fs)
                return (
                    FailingSource(source),
                    database_identity,
                    family_identity,
                )

            with patch.object(
                store,
                "_connect",
                side_effect=connect_with_failing_backup,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected primary backup failure",
                ) as raised:
                    store.backup_to(destination)

            notes = "\n".join(getattr(raised.exception, "__notes__", ()))
            self.assertIn("harness.db-journal: hard-linked-target", notes)

    def test_backup_failure_preserves_unverified_persist_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            harness_db.init_runtime(root)
            destination = base / "snapshot.db"
            store = SqliteStore(root)
            real_connect = store._connect

            class PersistJournalFailingSource:
                def __init__(self, delegate) -> None:
                    self.delegate = delegate

                def __getattr__(self, name):
                    return getattr(self.delegate, name)

                def backup(self, target) -> None:
                    target.execute("pragma journal_mode = persist")
                    target.execute("create table partial(value text)")
                    target.execute("insert into partial values ('incomplete')")
                    target.commit()
                    raise RuntimeError("injected persist-journal backup failure")

            def connect_with_failing_backup(project_fs):
                source, database_identity, family_identity = real_connect(project_fs)
                return (
                    PersistJournalFailingSource(source),
                    database_identity,
                    family_identity,
                )

            with patch.object(
                store,
                "_connect",
                side_effect=connect_with_failing_backup,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected persist-journal backup failure",
                ) as raised:
                    store.backup_to(destination)

            self.assertFalse(destination.exists())
            remaining = tuple(base.glob(".snapshot.db.kafa-backup-*.tmp*"))
            self.assertEqual(len(remaining), 1)
            self.assertTrue(remaining[0].name.endswith(".tmp-journal"))
            self.assertIn(
                "unverified temporary backup sidecar retained",
                "\n".join(getattr(raised.exception, "__notes__", ())),
            )

    def test_backup_cleanup_preserves_exchanged_temporary_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            harness_db.init_runtime(root)
            destination = base / "snapshot.db"
            external = base / "outside-temp-journal.bin"
            external.write_bytes(b"outside-temp-journal\n")
            external_before = external_identity(external)
            store = SqliteStore(root)
            real_connect = store._connect
            exchanged_sidecars: list[Path] = []
            blocked_exchanges: list[OSError] = []

            class ExchangedJournalFailingSource:
                def __init__(self, delegate) -> None:
                    self.delegate = delegate

                def __getattr__(self, name):
                    return getattr(self.delegate, name)

                def backup(self, target) -> None:
                    target.execute("pragma journal_mode = persist")
                    target.execute("create table partial(value text)")
                    target.execute("insert into partial values ('incomplete')")
                    target.commit()
                    database_path = Path(
                        target.execute("pragma database_list").fetchone()[2]
                    )
                    journal = Path(f"{database_path}-journal")
                    if not journal.exists():
                        raise AssertionError("SQLite PERSIST journal was not created")
                    try:
                        journal.unlink()
                    except OSError as exc:
                        if os.name != "nt":
                            raise
                        if not isinstance(exc, PermissionError) or getattr(
                            exc,
                            "winerror",
                            None,
                        ) not in {5, 32}:
                            raise
                        blocked_exchanges.append(exc)
                        raise RuntimeError(
                            "injected exchanged-journal backup failure"
                        ) from exc
                    journal.symlink_to(external)
                    exchanged_sidecars.append(journal)
                    raise RuntimeError("injected exchanged-journal backup failure")

            def connect_with_failing_backup(project_fs):
                source, database_identity, family_identity = real_connect(project_fs)
                return (
                    ExchangedJournalFailingSource(source),
                    database_identity,
                    family_identity,
                )

            with patch.object(
                store,
                "_connect",
                side_effect=connect_with_failing_backup,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected exchanged-journal backup failure",
                ) as raised:
                    store.backup_to(destination)

            notes = "\n".join(getattr(raised.exception, "__notes__", ()))
            self.assertFalse(destination.exists())
            remaining = tuple(base.glob(".snapshot.db.kafa-backup-*.tmp*"))
            self.assertEqual(len(remaining), 1)
            self.assertTrue(remaining[0].name.endswith(".tmp-journal"))
            if os.name == "nt":
                self.assertEqual(len(blocked_exchanges), 1)
                self.assertEqual(exchanged_sidecars, [])
                self.assertTrue(remaining[0].is_file())
                self.assertFalse(remaining[0].is_symlink())
                self.assertIn("unverified temporary backup sidecar retained", notes)
                self.assertEqual(external_identity(external), external_before)
                return
            self.assertIn("temporary backup sidecar cleanup failed", notes)
            self.assertIn("unsafe-target", notes)
            self.assertEqual(len(exchanged_sidecars), 1)
            self.assertTrue(exchanged_sidecars[0].is_symlink())
            self.assertEqual(external_identity(external), external_before)

    def test_backup_cleanup_does_not_claim_exchanged_regular_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            harness_db.init_runtime(root)
            destination = base / "snapshot.db"
            external = base / "outside-regular-temp-journal.bin"
            external.write_bytes(b"outside-regular-temp-journal\n")
            external_before = external_identity(external)
            store = SqliteStore(root)
            real_connect = store._connect
            exchanged_sidecars: list[Path] = []
            blocked_exchanges: list[OSError] = []

            class RegularJournalFailingSource:
                def __init__(self, delegate) -> None:
                    self.delegate = delegate

                def __getattr__(self, name):
                    return getattr(self.delegate, name)

                def backup(self, target) -> None:
                    target.execute("pragma journal_mode = persist")
                    target.execute("create table partial(value text)")
                    target.execute("insert into partial values ('incomplete')")
                    target.commit()
                    database_path = Path(
                        target.execute("pragma database_list").fetchone()[2]
                    )
                    journal = Path(f"{database_path}-journal")
                    try:
                        journal.unlink()
                    except OSError as exc:
                        if os.name != "nt":
                            raise
                        if not isinstance(exc, PermissionError) or getattr(
                            exc,
                            "winerror",
                            None,
                        ) not in {5, 32}:
                            raise
                        blocked_exchanges.append(exc)
                        raise RuntimeError(
                            "injected regular-journal backup failure"
                        ) from exc
                    external.rename(journal)
                    exchanged_sidecars.append(journal)
                    raise RuntimeError("injected regular-journal backup failure")

            def connect_with_failing_backup(project_fs):
                source, database_identity, family_identity = real_connect(project_fs)
                return (
                    RegularJournalFailingSource(source),
                    database_identity,
                    family_identity,
                )

            with patch.object(
                store,
                "_connect",
                side_effect=connect_with_failing_backup,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected regular-journal backup failure",
                ) as raised:
                    store.backup_to(destination)

            self.assertFalse(destination.exists())
            remaining = tuple(base.glob(".snapshot.db.kafa-backup-*.tmp*"))
            self.assertEqual(len(remaining), 1)
            self.assertTrue(remaining[0].name.endswith(".tmp-journal"))
            if os.name == "nt":
                self.assertEqual(len(blocked_exchanges), 1)
                self.assertEqual(exchanged_sidecars, [])
                self.assertTrue(remaining[0].is_file())
                self.assertFalse(remaining[0].is_symlink())
                self.assertEqual(external_identity(external), external_before)
                self.assertIn(
                    "unverified temporary backup sidecar retained",
                    "\n".join(getattr(raised.exception, "__notes__", ())),
                )
                return
            self.assertEqual(len(exchanged_sidecars), 1)
            self.assertEqual(
                external_identity(exchanged_sidecars[0]),
                external_before,
            )
            self.assertIn(
                "unverified temporary backup sidecar retained",
                "\n".join(getattr(raised.exception, "__notes__", ())),
            )

    def test_backup_close_failure_does_not_publish_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            harness_db.init_runtime(root)
            destination = base / "snapshot.db"
            store = SqliteStore(root)
            real_connect = store._connect

            class CloseFailingSource:
                def __init__(self, delegate) -> None:
                    self.delegate = delegate

                def __getattr__(self, name):
                    return getattr(self.delegate, name)

                def close(self) -> None:
                    self.delegate.close()
                    raise RuntimeError("injected source close failure")

            def connect_with_close_failure(project_fs):
                source, database_identity, family_identity = real_connect(project_fs)
                return (
                    CloseFailingSource(source),
                    database_identity,
                    family_identity,
                )

            with patch.object(
                store,
                "_connect",
                side_effect=connect_with_close_failure,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected source close failure",
                ):
                    store.backup_to(destination)

            self.assertFalse(destination.exists())
            self.assertEqual(
                tuple(base.glob(".snapshot.db.kafa-backup-*.tmp*")),
                (),
            )

    def test_backup_rejects_source_as_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            source = root / ".ai-team/state/harness.db"
            before = external_identity(source)

            with self.assertRaisesRegex(
                Exception,
                "destination overlaps the source database family",
            ):
                SqliteStore(root).backup_to(source)

            self.assertEqual(external_identity(source), before)

    def test_backup_rejects_source_sidecars_as_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            store = SqliteStore(root)

            for relative in store._db_family()[1:]:
                with self.subTest(relative=relative):
                    with self.assertRaisesRegex(
                        Exception,
                        "destination overlaps the source database family",
                    ):
                        store.backup_to(root / relative)

    def test_backup_rejects_case_only_source_family_aliases(self) -> None:
        for relative in SqliteStore._db_family():
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                harness_db.init_runtime(root)
                source = root / ".ai-team/state/harness.db"
                before = external_identity(source)
                alias = Path(*(part.swapcase() for part in relative.parts))

                with self.assertRaisesRegex(
                    Exception,
                    "destination overlaps the source database family",
                ):
                    SqliteStore(root).backup_to(root / alias)

                self.assertEqual(external_identity(source), before)
                self.assertEqual(
                    tuple(root.rglob("*.kafa-backup-*.tmp*")),
                    (),
                )

    @unittest.skipIf(os.name == "nt", "POSIX root alias contract")
    def test_backup_rejects_source_family_through_second_root_alias(self) -> None:
        for relative in SqliteStore._db_family():
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                root = base / "project"
                alias = base / "project-alias"
                harness_db.init_runtime(root)
                alias.symlink_to(root, target_is_directory=True)

                with self.assertRaisesRegex(
                    Exception,
                    "destination overlaps the source database family",
                ):
                    SqliteStore(root).backup_to(alias / relative)

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
                patch("core.execution.shutil.which", return_value=_FAKE_DOCKER),
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

    def test_container_structured_links_never_persist_verification_facts(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        for attack in ("preexisting", "produced"):
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
                        "CONTAINER-STRUCTURED",
                        "--kind",
                        "unit",
                        "--command-template",
                        command,
                        "--result-format",
                        "pytest-json",
                        "--result-path",
                        result_path,
                        "--requires-sandbox",
                        "--requires-no-network",
                    ),
                    (
                        "test-target",
                        "qualify",
                        "--id",
                        "CONTAINER-STRUCTURED-Q1",
                        "--target",
                        "CONTAINER-STRUCTURED",
                        "--acceptance",
                        "AC1",
                        "--rationale",
                        "container structured result proves AC1",
                        "--by",
                        "test-fixture",
                    ),
                ):
                    result = run_harness(root, *args)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

                passing = root / ".ai-team/runtime/passing-summary.json"
                passing.parent.mkdir(parents=True, exist_ok=True)
                passing.write_text(
                    '{"summary":{"total":1,"passed":1,"failed":0,"errors":0}}',
                    encoding="utf-8",
                )
                before = external_identity(passing)
                declared = root / result_path
                if attack == "preexisting":
                    declared.parent.mkdir(parents=True, exist_ok=True)
                    declared.symlink_to(passing)

                container_runs = 0

                def handle_container_run(argv, **kwargs):
                    nonlocal container_runs
                    mounts = [
                        value
                        for value in argv
                        if str(value).endswith(":/artifacts:rw")
                    ]
                    self.assertEqual(len(mounts), 1)
                    container_runs += 1
                    artifact_dir = Path(mounts[0].removesuffix(":/artifacts:rw"))
                    (artifact_dir / "stdout.txt").write_text(
                        "container command completed\n",
                        encoding="utf-8",
                    )
                    script = str(argv[-1])
                    structured = artifact_dir / "structured-result"
                    if (
                        "cp -a .ai-team/runtime/input/result.json "
                        "/artifacts/structured-result"
                    ) in script:
                        structured.symlink_to(passing)
                    else:
                        structured.write_bytes(passing.read_bytes())
                    return subprocess.CompletedProcess(argv, 0, "", "")

                def fake_container_run(argv, **kwargs):
                    return fake_local_docker_subprocess(
                        argv,
                        run_handler=handle_container_run,
                        **kwargs,
                    )

                with (
                    patch("core.execution.shutil.which", return_value=_FAKE_DOCKER),
                    patch(
                        "core.execution.subprocess.run",
                        side_effect=fake_container_run,
                    ),
                ):
                    with self.assertRaisesRegex(Exception, "unsafe-project-path"):
                        harness_db.verify_run(
                            root,
                            "CONTAINER-STRUCTURED",
                            acceptance="AC1",
                            runner="container",
                        )

                self.assertEqual(container_runs, 0 if attack == "preexisting" else 1)
                self.assertEqual(external_identity(passing), before)
                self.assertEqual(execution_fact_counts(root), (0, 0, 0))

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
                    (
                        "test-target",
                        "qualify",
                        "--id",
                        "STRUCTURED-Q1",
                        "--target",
                        "STRUCTURED",
                        "--acceptance",
                        "AC1",
                        "--rationale",
                        "structured result proves AC1",
                        "--by",
                        "test-fixture",
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

            def handle_container_run(argv, **kwargs):
                mounts = [value for value in argv if str(value).endswith(":/artifacts:rw")]
                self.assertEqual(len(mounts), 1)
                mount = mounts[0]
                artifact_dir = Path(mount.removesuffix(":/artifacts:rw"))
                (artifact_dir / "stdout.txt").write_text(
                    "Ran 1 test in 0.001s\nOK\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(argv, 0, "", "")

            def fake_container_run(argv, **kwargs):
                return fake_local_docker_subprocess(
                    argv,
                    run_handler=handle_container_run,
                    **kwargs,
                )

            with (
                patch("core.execution.uuid", fake_uuid),
                patch("core.execution.shutil.which", return_value=_FAKE_DOCKER),
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

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink primitive unavailable")
    def test_container_output_staging_never_mounts_or_writes_canonical_artifact_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            root = base / "project"
            root.mkdir()
            create_candidate(root)
            initialize_target(root)
            execution_id = "exchanged-container-artifacts"
            artifact_dir = root / ".ai-team/runtime/executions" / execution_id
            outside = base / "outside-container-artifacts"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_bytes(b"outside-must-stay-unchanged\n")
            before = external_identity(sentinel)
            fake_uuid = types.SimpleNamespace(
                uuid4=lambda: types.SimpleNamespace(hex=execution_id)
            )
            def handle_container_run(argv, **kwargs):
                mounts = [value for value in argv if str(value).endswith(":/artifacts:rw")]
                self.assertEqual(len(mounts), 1)
                mounted_dir = Path(mounts[0].removesuffix(":/artifacts:rw"))
                self.assertFalse(mounted_dir.is_relative_to(root))
                self.assertFalse(artifact_dir.exists())
                artifact_dir.parent.mkdir(parents=True, exist_ok=True)
                artifact_dir.symlink_to(outside, target_is_directory=True)
                (mounted_dir / "stdout.txt").write_text(
                    "Ran 1 test in 0.001s\nOK\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(argv, 0, "", "")

            def fake_container_run(argv, **kwargs):
                return fake_local_docker_subprocess(
                    argv,
                    run_handler=handle_container_run,
                    **kwargs,
                )

            with (
                patch("core.execution.uuid", fake_uuid),
                patch("core.execution.shutil.which", return_value=_FAKE_DOCKER),
                patch("core.execution.subprocess.run", side_effect=fake_container_run),
            ):
                with self.assertRaisesRegex(
                    Exception,
                    rf"unsafe-project-path: \.ai-team/runtime/executions/{execution_id}/stdout\.txt: unsafe-ancestor",
                ):
                    harness_db.verify_run(
                        root,
                        "UNIT",
                        acceptance="AC1",
                        runner="container",
                    )

            self.assertEqual(external_identity(sentinel), before)
            self.assertFalse((outside / "stdout.txt").exists())
            self.assertEqual(execution_fact_counts(root), (0, 0, 0))

    def test_container_verify_rejects_late_artifact_leaf_creation_without_facts(self) -> None:
        from core.project_fs import ProjectFS

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            create_candidate(root)
            initialize_target(root)
            execution_id = "late-exchanged-container-artifacts"
            artifact_relative = Path(
                f".ai-team/runtime/executions/{execution_id}/stdout.txt"
            )
            artifact = root / artifact_relative
            fake_uuid = types.SimpleNamespace(
                uuid4=lambda: types.SimpleNamespace(hex=execution_id)
            )
            original_atomic_write = ProjectFS.atomic_write
            publication_attempts = 0

            def handle_container_run(argv, **kwargs):
                mounts = [value for value in argv if str(value).endswith(":/artifacts:rw")]
                self.assertEqual(len(mounts), 1)
                mounted_dir = Path(mounts[0].removesuffix(":/artifacts:rw"))
                (mounted_dir / "stdout.txt").write_text(
                    "Ran 0 tests in 0.001s\nFAILED\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(argv, 0, "", "")

            def fake_container_run(argv, **kwargs):
                return fake_local_docker_subprocess(
                    argv,
                    run_handler=handle_container_run,
                    **kwargs,
                )

            def create_leaf_before_publication(
                active_fs,
                relative,
                data,
                *,
                mode=0o600,
                expected_destination=None,
            ):
                nonlocal publication_attempts
                if Path(relative) == artifact_relative:
                    publication_attempts += 1
                    artifact.parent.mkdir(parents=True, exist_ok=True)
                    artifact.write_bytes(b"late-regular-file-must-survive\n")
                return original_atomic_write(
                    active_fs,
                    relative,
                    data,
                    mode=mode,
                    expected_destination=expected_destination,
                )

            with (
                patch("core.execution.uuid", fake_uuid),
                patch("core.execution.shutil.which", return_value=_FAKE_DOCKER),
                patch("core.execution.subprocess.run", side_effect=fake_container_run),
                patch.object(
                    ProjectFS,
                    "atomic_write",
                    autospec=True,
                    side_effect=create_leaf_before_publication,
                ),
            ):
                with self.assertRaisesRegex(
                    Exception,
                    rf"unsafe-project-path: {artifact_relative.as_posix()}: path-identity-changed",
                ):
                    harness_db.verify_run(
                        root,
                        "UNIT",
                        acceptance="AC1",
                        runner="container",
                    )

            self.assertEqual(publication_attempts, 1)
            self.assertEqual(
                artifact.read_bytes(),
                b"late-regular-file-must-survive\n",
            )
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

    def test_precommit_validation_rejects_artifact_exchange_without_facts(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_candidate(root)
            initialize_target(root)
            second_validation_complete = threading.Event()
            exchange_complete = threading.Event()
            artifact_ready: list[Path] = []
            original_validate = execution_module.validate_execution_result
            calls = 0

            def validate_then_pause(*args, **kwargs):
                nonlocal calls
                calls += 1
                original_validate(*args, **kwargs)
                if calls == 2:
                    result = args[2]
                    artifact_ready.append(root / result.artifact_path)
                    second_validation_complete.set()
                    if not exchange_complete.wait(5):
                        raise AssertionError("artifact exchange did not complete")

            def exchange_artifact() -> None:
                if not second_validation_complete.wait(5):
                    return
                artifact = artifact_ready[0]
                victim = root / ".ai-team/runtime/precommit-artifact.txt"
                victim.parent.mkdir(parents=True, exist_ok=True)
                victim.write_bytes(artifact.read_bytes())
                artifact.unlink()
                artifact.symlink_to(victim)
                exchange_complete.set()

            exchanger = threading.Thread(target=exchange_artifact)
            exchanger.start()
            self.addCleanup(exchanger.join, 5)
            with patch.object(
                execution_module,
                "validate_execution_result",
                side_effect=validate_then_pause,
            ):
                with self.assertRaisesRegex(Exception, "unsafe-project-path"):
                    harness_db.verify_run(root, "UNIT", acceptance="AC1")
            exchanger.join(5)
            self.assertFalse(exchanger.is_alive())
            self.assertGreaterEqual(calls, 3)
            self.assertEqual(execution_fact_counts(root), (0, 0, 0))

    def test_delivery_rejects_linked_execution_artifact(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        for link_kind in ("symlink", "hardlink"):
            with self.subTest(link_kind=link_kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                create_candidate(root)
                initialize_target(root)
                harness_db.verify_run(root, "UNIT", acceptance="AC1")
                with harness_db.connection(root) as conn:
                    execution = conn.execute(
                        "select * from executions order by created_at desc limit 1"
                    ).fetchone()
                self.assertIsNotNone(execution)
                artifact = root / str(execution["artifact_path"])
                victim = root / ".ai-team/runtime/delivery-artifact-victim.txt"
                victim.parent.mkdir(parents=True, exist_ok=True)
                victim.write_bytes(artifact.read_bytes())
                artifact.unlink()
                if link_kind == "symlink":
                    artifact.symlink_to(victim)
                else:
                    os.link(victim, artifact)

                with harness_db.connection(root) as conn:
                    current = conn.execute(
                        "select * from executions where id = ?",
                        (execution["id"],),
                    ).fetchone()
                    issues = delivery_module.execution_issues(
                        conn,
                        root,
                        current,
                        str(current["candidate_sha"]),
                    )

                self.assertTrue(
                    any("unsafe-project-path" in issue for issue in issues),
                    issues,
                )

    def test_migrate_dry_run_rejects_dangling_database_symlink(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            state = root / ".ai-team/state"
            state.mkdir(parents=True)
            (state / "harness.db").symlink_to(base / "missing-external.db")

            with self.assertRaisesRegex(
                Exception,
                "unsafe-project-path: .ai-team/state/harness.db: unsafe-target",
            ):
                harness_db.migrate(root, "29", 30, dry_run=True)

    def test_migrate_staging_validation_rejects_leaf_exchange_before_copy(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            init_schema29_fixture(root)
            external = base / "outside-staging-copy.db"
            hook_called = False
            external_at_attack: list[tuple[bytes, str, int, int]] = []

            def exchange_staging(active_fs, relative: Path) -> None:
                nonlocal hook_called
                hook_called = True
                staging = active_fs.absolute(relative)
                parked = staging.with_name(staging.name + ".parked")
                staging.rename(parked)
                external.write_bytes(parked.read_bytes())
                staging.symlink_to(external)
                external_at_attack.append(external_identity(external))

            with patch.object(
                harness_db,
                "_before_staging_validation_snapshot_read",
                side_effect=exchange_staging,
                create=True,
            ):
                with self.assertRaisesRegex(Exception, "unsafe-project-path"):
                    harness_db.migrate(root, "29", 31)

            self.assertTrue(hook_called)
            self.assertEqual(
                external_identity(external),
                external_at_attack[0],
            )
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                self.assertEqual(
                    conn.execute(
                        "select schema_version from project where id=1"
                    ).fetchone()[0],
                    29,
                )

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

    def test_projection_rollback_unlink_rejects_regular_file_replacement(self) -> None:
        from core.project_fs import ProjectFS

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backup_directory = root / ".ai-team/backups/manual/projections"
            backup_directory.mkdir(parents=True)
            target_relative = PROJECTION_ROLLBACK_PATHS[0]
            target = root / target_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"migration-generated\n")
            parked = target.with_name(target.name + ".parked")
            replacement = b"replacement-must-survive\n"
            metadata = {
                "directory": str(backup_directory),
                "entries": [
                    {"path": relative.as_posix(), "existed": False}
                    for relative in PROJECTION_ROLLBACK_PATHS
                ],
            }
            original_unlink = ProjectFS.unlink_regular
            raced = False

            def replace_before_unlink(
                active_fs,
                relative,
                *,
                missing_ok=False,
                expected=None,
            ):
                nonlocal raced
                if Path(relative) == target_relative and not raced:
                    raced = True
                    target.rename(parked)
                    target.write_bytes(replacement)
                return original_unlink(
                    active_fs,
                    relative,
                    missing_ok=missing_ok,
                    expected=expected,
                )

            with patch.object(
                ProjectFS,
                "unlink_regular",
                autospec=True,
                side_effect=replace_before_unlink,
            ):
                with self.assertRaisesRegex(
                    Exception,
                    "path-identity-changed",
                ):
                    local_core_migration._restore_projection_backup(
                        root,
                        metadata,
                    )

            self.assertTrue(raced)
            self.assertEqual(target.read_bytes(), replacement)
            self.assertEqual(parked.read_bytes(), b"migration-generated\n")

    def test_migration_sentinel_cleanup_rejects_regular_file_replacement(self) -> None:
        from core.project_fs import ProjectFS

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            sentinel_relative = Path(
                ".ai-team/state/local-core-migration.lock"
            )
            sentinel = root / sentinel_relative
            parked = sentinel.with_name(sentinel.name + ".parked")
            replacement = b"replacement-sentinel-must-survive\n"
            original_unlink = ProjectFS.unlink_regular
            raced = False

            def replace_before_unlink(
                active_fs,
                relative,
                *,
                missing_ok=False,
                expected=None,
            ):
                nonlocal raced
                if Path(relative) == sentinel_relative and not raced:
                    raced = True
                    sentinel.rename(parked)
                    sentinel.write_bytes(replacement)
                return original_unlink(
                    active_fs,
                    relative,
                    missing_ok=missing_ok,
                    expected=expected,
                )

            with patch.object(
                ProjectFS,
                "unlink_regular",
                autospec=True,
                side_effect=replace_before_unlink,
            ):
                with self.assertRaisesRegex(
                    Exception,
                    "path-identity-changed",
                ):
                    with local_core_migration._project_migration_lock(root):
                        pass

            self.assertTrue(raced)
            self.assertEqual(sentinel.read_bytes(), replacement)
            self.assertTrue(parked.is_file())

    def test_empty_sidecar_cleanup_rejects_regular_file_replacement(self) -> None:
        from core.project_fs import ProjectFS

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = root / ".ai-team/state/harness.db"
            active.parent.mkdir(parents=True)
            active.write_bytes(b"database-placeholder\n")
            wal = active.with_name(active.name + "-wal")
            wal.write_bytes(b"")
            parked = wal.with_name(wal.name + ".parked")
            replacement = b"replacement-sidecar-must-survive\n"
            original_unlink = ProjectFS.unlink_regular
            raced = False

            def replace_before_unlink(
                active_fs,
                relative,
                *,
                missing_ok=False,
                expected=None,
            ):
                nonlocal raced
                if Path(relative).name == wal.name and not raced:
                    raced = True
                    wal.rename(parked)
                    wal.write_bytes(replacement)
                return original_unlink(
                    active_fs,
                    relative,
                    missing_ok=missing_ok,
                    expected=expected,
                )

            with patch.object(
                ProjectFS,
                "unlink_regular",
                autospec=True,
                side_effect=replace_before_unlink,
            ):
                with self.assertRaisesRegex(
                    Exception,
                    "path-identity-changed",
                ):
                    local_core_migration._remove_empty_active_sidecars(active)

            self.assertTrue(raced)
            self.assertEqual(wal.read_bytes(), replacement)
            self.assertEqual(parked.read_bytes(), b"")

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

    def test_projection_restore_rejects_safe_replacement_at_write_boundary(self) -> None:
        from core.project_fs import ProjectFS

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / PROJECTION_PATHS[0]
            target.parent.mkdir(parents=True)
            target.write_bytes(b"schema_version: 29\nstate: original\n")
            backup_dir = root / ".ai-team/backups/manual"
            backup_dir.mkdir(parents=True)
            projection_backup = local_core_migration._create_projection_backup(
                root,
                backup_dir,
            )
            target.write_bytes(b"schema_version: 30\nstate: changed\n")
            parked = root / "projection-before-race.yaml"
            replacement = root / "projection-raced.yaml"
            replacement.write_bytes(b"schema_version: 30\nstate: raced\n")
            original_atomic_write = ProjectFS.atomic_write
            raced = False

            def race_then_restore(
                active_fs,
                relative,
                data,
                *,
                mode=0o600,
                expected_destination=None,
            ):
                nonlocal raced
                if Path(relative) == PROJECTION_PATHS[0] and not raced:
                    target.rename(parked)
                    replacement.rename(target)
                    raced = True
                return original_atomic_write(
                    active_fs,
                    relative,
                    data,
                    mode=mode,
                    expected_destination=expected_destination,
                )

            with patch.object(
                ProjectFS,
                "atomic_write",
                autospec=True,
                side_effect=race_then_restore,
            ):
                with self.assertRaisesRegex(
                    Exception,
                    "path-identity-changed",
                ):
                    local_core_migration._restore_projection_backup(
                        root,
                        projection_backup,
                    )

            self.assertTrue(raced)
            self.assertEqual(
                target.read_bytes(),
                b"schema_version: 30\nstate: raced\n",
            )
            self.assertEqual(
                parked.read_bytes(),
                b"schema_version: 30\nstate: changed\n",
            )

    def test_migration_activation_rejects_stale_source_and_destination_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / ".ai-team/state"
            state.mkdir(parents=True)
            active = state / "harness.db"
            staging = state / "staging.db"
            active.write_bytes(b"active-original\n")
            staging.write_bytes(b"staging-verified\n")

            with project_fs(root) as fs:
                active_relative = Path(".ai-team/state/harness.db")
                staging_relative = Path(".ai-team/state/staging.db")
                active_receipt = fs._snapshot(
                    active_relative,
                    allow_missing=False,
                )
                staging_receipt = fs._snapshot(
                    staging_relative,
                    allow_missing=False,
                )
                raced_active = state / "raced-active.db"
                raced_active.write_bytes(b"active-raced\n")
                os.replace(raced_active, active)
                with self.assertRaisesRegex(
                    Exception,
                    r"harness\.db: path-identity-changed",
                ):
                    local_core_migration._activate_staging_database(
                        fs,
                        staging_relative,
                        active_relative,
                        staging_snapshot=staging_receipt,
                        active_snapshot=active_receipt,
                    )

            self.assertEqual(active.read_bytes(), b"active-raced\n")
            self.assertEqual(staging.read_bytes(), b"staging-verified\n")

            active.write_bytes(b"active-second\n")
            staging.write_bytes(b"staging-second\n")
            with project_fs(root) as fs:
                active_receipt = fs._snapshot(
                    active_relative,
                    allow_missing=False,
                )
                staging_receipt = fs._snapshot(
                    staging_relative,
                    allow_missing=False,
                )
                raced_staging = state / "raced-staging.db"
                raced_staging.write_bytes(b"staging-raced\n")
                os.replace(raced_staging, staging)
                with self.assertRaisesRegex(
                    Exception,
                    r"staging\.db: path-identity-changed",
                ):
                    local_core_migration._activate_staging_database(
                        fs,
                        staging_relative,
                        active_relative,
                        staging_snapshot=staging_receipt,
                        active_snapshot=active_receipt,
                    )

            self.assertEqual(active.read_bytes(), b"active-second\n")
            self.assertEqual(staging.read_bytes(), b"staging-raced\n")

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
                with project_fs(root) as fs:
                    active_snapshot = fs._snapshot(
                        Path(".ai-team/state/harness.db"),
                        allow_missing=False,
                    )
                local_core_migration._preserve_failed_schema30(
                    active,
                    failed,
                    expected_active_snapshot=active_snapshot,
                )

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
                with project_fs(root) as fs:
                    active_snapshot = fs._snapshot(
                        Path(".ai-team/state/harness.db"),
                        allow_missing=False,
                    )
                    backup_snapshot = fs._snapshot(
                        fs.relative_to_root(Path(backup.backup_path)),
                        allow_missing=False,
                    )
                local_core_migration._restore_verified_backup(
                    active,
                    backup,
                    expected_active_snapshot=active_snapshot,
                    expected_backup_snapshot=backup_snapshot,
                )

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

    def test_migration_uses_fallback_manifest_when_canonical_manifest_becomes_unsafe(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            init_schema29_fixture(root)
            active = root / ".ai-team/state/harness.db"
            external = base / "outside-migration-manifest.json"
            external.write_bytes(b'{"outside":true}\n')
            before = external_identity(external)

            def attack_manifest_after_activation(_active: Path) -> None:
                backup_dir = next(
                    (root / ".ai-team/backups").glob(
                        "schema-29-before-local-core-*"
                    )
                )
                manifest = backup_dir / "migration-manifest.json"
                manifest.rename(backup_dir / "migration-manifest.staged.json")
                manifest.symlink_to(external)
                raise RuntimeError("injected manifest identity failure")

            caught: BaseException | None = None
            try:
                local_core_migration.migrate_project_to_schema30(
                    root,
                    active_validator=attack_manifest_after_activation,
                )
            except BaseException as exc:
                caught = exc

            self.assertIsNotNone(caught)
            self.assertIn("injected manifest identity failure", str(caught))
            backup_dir = next(
                (root / ".ai-team/backups").glob(
                    "schema-29-before-local-core-*"
                )
            )
            fallback_paths = tuple(
                (root / ".ai-team/state").glob(
                    "migration-recovery-*.json"
                )
            )
            self.assertEqual(len(fallback_paths), 1)
            fallback = __import__("json").loads(
                fallback_paths[0].read_text(encoding="utf-8")
            )
            self.assertEqual(fallback["status"], "rollback-incomplete")
            self.assertIn("injected manifest identity failure", fallback["error"])
            self.assertIn("unsafe-project-path", fallback["manifest_write_error"])
            self.assertEqual(
                Path(fallback["recovery_manifest_path"]).resolve(),
                fallback_paths[0].resolve(),
            )
            sentinel = __import__("json").loads(
                (root / ".ai-team/state/local-core-migration.lock").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                Path(sentinel["manifest_path"]).resolve(),
                fallback_paths[0].resolve(),
            )
            self.assertEqual(external_identity(external), before)
            with closing(sqlite3.connect(active)) as conn:
                self.assertEqual(
                    conn.execute(
                        "select schema_version from project where id=1"
                    ).fetchone()[0],
                    29,
                )

    def test_migration_uses_state_fallback_when_backup_ancestor_becomes_unsafe(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            init_schema29_fixture(root)
            active = root / ".ai-team/state/harness.db"
            external = base / "outside-backup-directory"
            external.mkdir()
            external_before = (
                external.stat().st_ino,
                tuple(external.iterdir()),
            )
            parked_paths: list[Path] = []

            def attack_backup_ancestor_after_activation(_active: Path) -> None:
                backup_dir = next(
                    (root / ".ai-team/backups").glob(
                        "schema-29-before-local-core-*"
                    )
                )
                parked = backup_dir.with_name(backup_dir.name + ".parked")
                backup_dir.rename(parked)
                backup_dir.symlink_to(external, target_is_directory=True)
                parked_paths.append(parked)
                raise RuntimeError("injected backup ancestor identity failure")

            caught: BaseException | None = None
            try:
                local_core_migration.migrate_project_to_schema30(
                    root,
                    active_validator=attack_backup_ancestor_after_activation,
                )
            except BaseException as exc:
                caught = exc

            self.assertIsNotNone(caught)
            self.assertIn("database rollback failed", str(caught))
            fallback_paths = tuple(
                (root / ".ai-team/state").glob(
                    "migration-recovery-*.json"
                )
            )
            self.assertEqual(len(fallback_paths), 1)
            fallback = __import__("json").loads(
                fallback_paths[0].read_text(encoding="utf-8")
            )
            self.assertEqual(fallback["status"], "rollback-incomplete")
            self.assertIn(
                "injected backup ancestor identity failure",
                fallback["error"],
            )
            self.assertEqual(fallback["database_restore_status"], "failed")
            self.assertIn(
                "unsafe-project-path",
                fallback["database_restore_error"],
            )
            self.assertIn(
                "unsafe-project-path",
                fallback["manifest_write_error"],
            )
            sentinel = __import__("json").loads(
                (root / ".ai-team/state/local-core-migration.lock").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                Path(sentinel["manifest_path"]).resolve(),
                fallback_paths[0].resolve(),
            )
            self.assertEqual(
                (external.stat().st_ino, tuple(external.iterdir())),
                external_before,
            )
            parked_manifest = __import__("json").loads(
                (parked_paths[0] / "migration-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(parked_manifest["status"], "staged")
            with closing(sqlite3.connect(active)) as conn:
                self.assertEqual(
                    conn.execute(
                        "select schema_version from project where id=1"
                    ).fetchone()[0],
                    30,
                )

    def test_migration_checkpoint_rejects_journal_created_during_connect(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            active = root / ".ai-team/state/harness.db"
            journal = active.with_name(active.name + "-journal")
            journal.unlink(missing_ok=True)
            real_connect = sqlite3.connect
            injected = False

            def inject_journal_then_connect(*args, **kwargs):
                nonlocal injected
                if not injected:
                    journal.write_bytes(b"attacker-journal\n")
                    injected = True
                return real_connect(*args, **kwargs)

            with patch(
                "core.store.sqlite3.connect",
                side_effect=inject_journal_then_connect,
            ):
                with self.assertRaisesRegex(
                    Exception,
                    r"harness\.db-journal: path-identity-changed",
                ):
                    local_core_migration._checkpoint_active_database(active)

            self.assertTrue(injected)
            self.assertEqual(journal.read_bytes(), b"attacker-journal\n")

    def test_schema_backup_rejects_source_journal_created_during_connect(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            source = root / ".ai-team/state/harness.db"
            journal = source.with_name(source.name + "-journal")
            journal.unlink(missing_ok=True)
            real_connect = sqlite3.connect
            injected = False

            def inject_journal_then_connect(*args, **kwargs):
                nonlocal injected
                if not injected:
                    journal.write_bytes(b"attacker-backup-journal\n")
                    injected = True
                return real_connect(*args, **kwargs)

            with patch(
                "core.store.sqlite3.connect",
                side_effect=inject_journal_then_connect,
            ):
                with self.assertRaisesRegex(
                    Exception,
                    r"harness\.db-journal: path-identity-changed",
                ):
                    schema_lifecycle.backup_sqlite_database(
                        root,
                        expected_source_version=29,
                    )

            self.assertTrue(injected)
            self.assertEqual(journal.read_bytes(), b"attacker-backup-journal\n")

    def test_non_wal_verified_connections_reject_sidecar_created_after_mode_probe(self) -> None:
        cases = (
            ("immutable", {"access": "ro", "immutable": True}),
            ("memory", {"access": "rw", "journal_mode": "memory"}),
        )
        for name, connection_options in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                database = root / "probe.db"
                with closing(sqlite3.connect(database)) as conn:
                    conn.execute("create table facts(id integer primary key)")
                    conn.commit()
                injected_sidecar = database.with_name(database.name + "-wal")
                real_connect = sqlite3.connect
                injected = False

                class ConnectionProxy:
                    def __init__(self, delegate) -> None:
                        object.__setattr__(self, "delegate", delegate)

                    def __getattr__(self, attribute):
                        return getattr(self.delegate, attribute)

                    def __setattr__(self, attribute, value) -> None:
                        if attribute == "delegate":
                            object.__setattr__(self, attribute, value)
                        else:
                            setattr(self.delegate, attribute, value)

                    def execute(self, sql, *args, **kwargs):
                        nonlocal injected
                        result = self.delegate.execute(sql, *args, **kwargs)
                        if (
                            str(sql).strip().lower() == "pragma schema_version"
                            and not injected
                        ):
                            injected_sidecar.write_bytes(
                                f"attacker-{name}-wal\n".encode("utf-8")
                            )
                            injected = True
                        return result

                    def close(self) -> None:
                        self.delegate.close()

                with (
                    project_fs(root) as active_fs,
                    patch(
                        "core.store.sqlite3.connect",
                        side_effect=lambda *args, **kwargs: ConnectionProxy(
                            real_connect(*args, **kwargs)
                        ),
                    ),
                ):
                    with self.assertRaisesRegex(
                        Exception,
                        r"probe\.db-wal: path-identity-changed",
                    ):
                        with store_module._verified_sqlite_connection(
                            active_fs,
                            Path("probe.db"),
                            **connection_options,
                        ) as verified:
                            verified.execute("select count(*) from facts").fetchone()

                self.assertTrue(injected)
                self.assertEqual(
                    injected_sidecar.read_bytes(),
                    f"attacker-{name}-wal\n".encode("utf-8"),
                )

    @unittest.skipUnless(hasattr(os, "link"), "hardlink primitive unavailable")
    def test_staging_rejects_hardlinked_journal_before_any_sql(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            init_schema29_fixture(root)
            source = root / ".ai-team/state/harness.db"
            staging = root / ".ai-team/state/harness.db.schema30.staging"
            staging_journal = staging.with_name(staging.name + "-journal")
            external = base / "outside-journal"
            external.write_bytes(b"")
            before = external_identity(external)
            real_connect = sqlite3.connect
            injected = False

            def inject_hardlink_then_connect(database, *args, **kwargs):
                nonlocal injected
                if not injected and "harness.db.schema30.staging" in str(database):
                    os.link(external, staging_journal)
                    injected = True
                return real_connect(database, *args, **kwargs)

            with patch(
                "core.store.sqlite3.connect",
                side_effect=inject_hardlink_then_connect,
            ):
                with self.assertRaisesRegex(
                    Exception,
                    r"harness\.db\.schema30\.staging-journal",
                ):
                    local_core_migration.stage_schema29_to_schema30(
                        source,
                        staging,
                        project_root=root,
                    )

            self.assertTrue(injected)
            self.assertEqual(external_identity(external), before)

    def test_schema_backup_retains_unverified_partial_sidecar_on_connect_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            real_connect = sqlite3.connect
            injected_paths: list[Path] = []

            def fail_partial_connect(database, *args, **kwargs):
                if "harness.db.partial" in str(database):
                    partial = next(
                        (root / ".ai-team/backups").glob(
                            "schema-29-before-local-core-*/harness.db.partial"
                        )
                    )
                    sidecar = partial.with_name(partial.name + "-journal")
                    sidecar.write_bytes(b"unverified-sidecar\n")
                    injected_paths.append(sidecar)
                    raise sqlite3.OperationalError("injected partial connect failure")
                return real_connect(database, *args, **kwargs)

            with patch(
                "core.store.sqlite3.connect",
                side_effect=fail_partial_connect,
            ):
                with self.assertRaisesRegex(
                    Exception,
                    r"harness\.db\.partial-journal: path-identity-changed",
                ) as raised:
                    schema_lifecycle.backup_sqlite_database(
                        root,
                        expected_source_version=29,
                    )

            self.assertEqual(len(injected_paths), 1)
            self.assertEqual(
                injected_paths[0].read_bytes(),
                b"unverified-sidecar\n",
            )
            self.assertIn(
                "cleanup-incomplete",
                "\n".join(getattr(raised.exception, "__notes__", ())),
            )
            self.assertIn(
                "injected partial connect failure",
                "\n".join(getattr(raised.exception, "__notes__", ())),
            )

    @unittest.skipUnless(hasattr(os, "link"), "hardlink primitive unavailable")
    def test_staging_memory_journal_blocks_pre_transaction_hardlink_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            init_schema29_fixture(root)
            source = root / ".ai-team/state/harness.db"
            staging = root / ".ai-team/state/harness.db.schema30.staging"
            staging_journal = staging.with_name(staging.name + "-journal")
            external = base / "outside-pre-transaction-journal"
            external.write_bytes(b"")
            before = external_identity(external)
            real_connect = sqlite3.connect
            injected = False

            class ConnectionProxy:
                def __init__(self, delegate, *, staging_connection: bool) -> None:
                    object.__setattr__(self, "delegate", delegate)
                    object.__setattr__(self, "staging_connection", staging_connection)

                def __getattr__(self, name):
                    return getattr(self.delegate, name)

                def __setattr__(self, name, value) -> None:
                    if name in {"delegate", "staging_connection"}:
                        object.__setattr__(self, name, value)
                    else:
                        setattr(self.delegate, name, value)

                def execute(self, sql, *args, **kwargs):
                    nonlocal injected
                    if (
                        self.staging_connection
                        and "begin immediate" in str(sql).lower()
                        and not injected
                    ):
                        os.link(external, staging_journal)
                        injected = True
                    return self.delegate.execute(sql, *args, **kwargs)

                def close(self) -> None:
                    self.delegate.close()

            def connect_with_proxy(database, *args, **kwargs):
                return ConnectionProxy(
                    real_connect(database, *args, **kwargs),
                    staging_connection=(
                        "harness.db.schema30.staging" in str(database)
                    ),
                )

            with patch(
                "core.store.sqlite3.connect",
                side_effect=connect_with_proxy,
            ):
                with self.assertRaisesRegex(
                    Exception,
                    r"harness\.db\.schema30\.staging-journal: hard-linked-target",
                ):
                    local_core_migration.stage_schema29_to_schema30(
                        source,
                        staging,
                        project_root=root,
                    )

            self.assertTrue(injected)
            self.assertEqual(external_identity(external), before)
            self.assertTrue(staging_journal.exists())

    @unittest.skipUnless(hasattr(os, "link"), "hardlink primitive unavailable")
    def test_backup_memory_journal_blocks_pre_backup_hardlink_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            init_schema29_fixture(root)
            external = base / "outside-pre-backup-journal"
            external.write_bytes(b"")
            before = external_identity(external)
            real_connect = sqlite3.connect
            injected_paths: list[Path] = []

            class ConnectionProxy:
                def __init__(self, delegate, database: str) -> None:
                    object.__setattr__(self, "delegate", delegate)
                    object.__setattr__(self, "database", database)

                def __getattr__(self, name):
                    return getattr(self.delegate, name)

                def __setattr__(self, name, value) -> None:
                    if name in {"delegate", "database"}:
                        object.__setattr__(self, name, value)
                    else:
                        setattr(self.delegate, name, value)

                def backup(self, target, *args, **kwargs):
                    partial = next(
                        (root / ".ai-team/backups").glob(
                            "schema-29-before-local-core-*/harness.db.partial"
                        )
                    )
                    sidecar = partial.with_name(partial.name + "-journal")
                    os.link(external, sidecar)
                    injected_paths.append(sidecar)
                    delegate_target = getattr(target, "delegate", target)
                    return self.delegate.backup(delegate_target, *args, **kwargs)

                def close(self) -> None:
                    self.delegate.close()

            def connect_with_proxy(database, *args, **kwargs):
                return ConnectionProxy(
                    real_connect(database, *args, **kwargs),
                    str(database),
                )

            with patch(
                "core.store.sqlite3.connect",
                side_effect=connect_with_proxy,
            ):
                with self.assertRaisesRegex(
                    Exception,
                    r"harness\.db\.partial-journal: hard-linked-target",
                ) as raised:
                    schema_lifecycle.backup_sqlite_database(
                        root,
                        expected_source_version=29,
                    )

            self.assertEqual(len(injected_paths), 1)
            self.assertEqual(external_identity(external), before)
            self.assertTrue(injected_paths[0].exists())
            self.assertIn(
                "cleanup-incomplete",
                "\n".join(getattr(raised.exception, "__notes__", ())),
            )


class ProjectFSContractRedTests(unittest.TestCase):
    def test_relative_path_grammar_rejects_escape_and_windows_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with project_fs(Path(temp)) as fs:
                invalid = (
                    Path("../outside"),
                    Path("/absolute"),
                    Path("C:/outside"),
                    Path("//server/share"),
                    "./dot-prefix.txt",
                    "nested//empty.txt",
                    "nested/./dot.txt",
                    Path("safe/name:stream"),
                    Path("CON"),
                    Path("trailing. "),
                )
                if os.name != "nt":
                    invalid += (
                        Path(r"..\outside.txt"),
                        Path(r"nested\file.txt"),
                    )
                for relative in invalid:
                    with self.subTest(relative=str(relative)):
                        with self.assertRaisesRegex(Exception, "invalid-relative-path"):
                            fs.audit((relative,))

    def test_trusted_windows_path_uses_closed_forward_slash_grammar(self) -> None:
        relative = PureWindowsPath(".ai-team/state/harness.db")
        with tempfile.TemporaryDirectory() as temp:
            with project_fs(Path(temp)) as fs:
                fs.audit((relative,), allow_missing=True)
                with self.assertRaisesRegex(Exception, "invalid-relative-path"):
                    fs.audit((r".ai-team\state\harness.db",), allow_missing=True)

    @unittest.skipIf(os.name == "nt", "POSIX root exchange contract")
    def test_project_fs_open_rejects_root_exchange_before_backend_pin(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            replacement = base / "replacement"
            detached = base / "detached"
            root.mkdir()
            replacement.mkdir()
            original_init = project_fs_module._PosixBackend.__init__
            exchanged = False

            def exchange_then_open(backend, resolved: Path) -> None:
                nonlocal exchanged
                if not exchanged:
                    root.rename(detached)
                    replacement.rename(root)
                    exchanged = True
                original_init(backend, resolved)

            with patch.object(
                project_fs_module._PosixBackend,
                "__init__",
                new=exchange_then_open,
            ):
                with self.assertRaisesRegex(Exception, "path-identity-changed"):
                    with project_fs(root) as fs:
                        fs.atomic_write(Path("must-not-exist.txt"), b"unsafe\n")

            self.assertTrue(exchanged)
            self.assertFalse((root / "must-not-exist.txt").exists())
            self.assertFalse((detached / "must-not-exist.txt").exists())

    @unittest.skipIf(os.name == "nt", "POSIX root exchange contract")
    def test_atomic_write_reports_root_exchange_during_publication(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            replacement = base / "replacement"
            detached = base / "detached"
            root.mkdir()
            replacement.mkdir()
            (replacement / "marker.txt").write_text("replacement\n", encoding="utf-8")
            real_rename_noreplace = project_fs_module._posix_rename_noreplace
            exchanged = False

            def exchange_root_inside_publish(*args, **kwargs):
                nonlocal exchanged
                if not exchanged:
                    root.rename(detached)
                    replacement.rename(root)
                    exchanged = True
                return real_rename_noreplace(*args, **kwargs)

            with (
                project_fs(root) as fs,
                patch.object(
                    project_fs_module,
                    "_posix_rename_noreplace",
                    side_effect=exchange_root_inside_publish,
                ),
            ):
                with self.assertRaisesRegex(Exception, "path-identity-changed"):
                    fs.atomic_write(Path("published.txt"), b"new\n")

            self.assertTrue(exchanged)
            self.assertEqual((root / "marker.txt").read_text(), "replacement\n")
            self.assertFalse((root / "published.txt").exists())
            self.assertEqual((detached / "published.txt").read_bytes(), b"new\n")

    @unittest.skipIf(os.name == "nt", "POSIX root exchange contract")
    def test_replace_file_reports_root_exchange_during_publication(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            replacement = base / "replacement"
            detached = base / "detached"
            root.mkdir()
            replacement.mkdir()
            (root / "source.txt").write_bytes(b"source\n")
            (replacement / "marker.txt").write_text("replacement\n", encoding="utf-8")
            real_rename_noreplace = project_fs_module._posix_rename_noreplace
            exchanged = False

            def exchange_root_inside_publish(*args, **kwargs):
                nonlocal exchanged
                if not exchanged:
                    root.rename(detached)
                    replacement.rename(root)
                    exchanged = True
                return real_rename_noreplace(*args, **kwargs)

            with (
                project_fs(root) as fs,
                patch.object(
                    project_fs_module,
                    "_posix_rename_noreplace",
                    side_effect=exchange_root_inside_publish,
                ),
            ):
                with self.assertRaisesRegex(Exception, "path-identity-changed"):
                    fs.replace_file(Path("source.txt"), Path("published.txt"))

            self.assertTrue(exchanged)
            self.assertEqual((root / "marker.txt").read_text(), "replacement\n")
            self.assertFalse((root / "published.txt").exists())
            self.assertEqual((detached / "published.txt").read_bytes(), b"source\n")

    @unittest.skipIf(os.name == "nt", "POSIX atomic exchange contract")
    def test_atomic_write_restores_leaf_exchanged_inside_final_syscall(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target.txt"
            parked = root / "parked-original.txt"
            attacker = root / "attacker.txt"
            target.write_bytes(b"original\n")
            attacker.write_bytes(b"attacker\n")
            real_exchange = project_fs_module._posix_rename_exchange
            exchanged = False

            def exchange_leaf_then_publish(*args):
                nonlocal exchanged
                if not exchanged:
                    target.rename(parked)
                    attacker.rename(target)
                    exchanged = True
                return real_exchange(*args)

            with (
                project_fs(root) as fs,
                patch.object(
                    project_fs_module,
                    "_posix_rename_exchange",
                    side_effect=exchange_leaf_then_publish,
                ),
            ):
                with self.assertRaisesRegex(Exception, "path-identity-changed"):
                    fs.atomic_write(Path("target.txt"), b"new\n")

            self.assertTrue(exchanged)
            self.assertEqual(target.read_bytes(), b"attacker\n")
            self.assertEqual(parked.read_bytes(), b"original\n")

    @unittest.skipIf(os.name == "nt", "POSIX atomic exchange contract")
    def test_atomic_write_restores_hardlink_raced_inside_final_syscall(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target.txt"
            parked = root / "parked-original.txt"
            attacker = root / "attacker.txt"
            alias = root / "attacker-alias.txt"
            target.write_bytes(b"original\n")
            attacker.write_bytes(b"attacker\n")
            os.link(attacker, alias)
            real_exchange = project_fs_module._posix_rename_exchange
            exchanged = False

            def exchange_hardlink_then_publish(*args):
                nonlocal exchanged
                if not exchanged:
                    target.rename(parked)
                    attacker.rename(target)
                    exchanged = True
                return real_exchange(*args)

            with project_fs(root) as fs:
                receipt = fs._snapshot(Path("target.txt"), allow_missing=False)
                with patch.object(
                    project_fs_module,
                    "_posix_rename_exchange",
                    side_effect=exchange_hardlink_then_publish,
                ):
                    with self.assertRaisesRegex(
                        Exception,
                        r"target\.txt: path-identity-changed",
                    ):
                        fs.atomic_write(
                            Path("target.txt"),
                            b"kafa-new\n",
                            expected_destination=receipt,
                        )

            self.assertTrue(exchanged)
            self.assertEqual(target.read_bytes(), b"attacker\n")
            self.assertTrue(target.samefile(alias))
            self.assertEqual(parked.read_bytes(), b"original\n")
            self.assertEqual(tuple(root.glob(".target.txt.kafa-*.tmp")), ())
            self.assertFalse(
                any(
                    path.is_file() and path.read_bytes() == b"kafa-new\n"
                    for path in root.iterdir()
                )
            )

    @unittest.skipIf(os.name == "nt", "POSIX post-exchange cleanup contract")
    def test_atomic_write_removes_candidate_when_cleanup_source_is_replaced(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target.txt"
            parked = root / "parked-original.txt"
            attacker = root / "attacker.txt"
            target.write_bytes(b"original\n")
            attacker.write_bytes(b"attacker\n")
            real_noreplace = project_fs_module._posix_rename_noreplace
            raced = False

            def replace_cleanup_source_then_quarantine(*args):
                nonlocal raced
                source_name = args[1]
                if not raced and ".target.txt.kafa-" in source_name:
                    (root / source_name).rename(parked)
                    attacker.rename(root / source_name)
                    raced = True
                return real_noreplace(*args)

            with (
                project_fs(root) as fs,
                patch.object(
                    project_fs_module,
                    "_posix_rename_noreplace",
                    side_effect=replace_cleanup_source_then_quarantine,
                ),
            ):
                with self.assertRaisesRegex(Exception, "path-identity-changed"):
                    fs.atomic_write(Path("target.txt"), b"kafa-new\n")

            self.assertTrue(raced)
            self.assertEqual(target.read_bytes(), b"attacker\n")
            self.assertEqual(parked.read_bytes(), b"original\n")
            self.assertEqual(tuple(root.glob(".target.txt.kafa-*.tmp")), ())
            self.assertFalse(
                any(
                    path.is_file() and path.read_bytes() == b"kafa-new\n"
                    for path in root.iterdir()
                )
            )

    @unittest.skipIf(os.name == "nt", "POSIX atomic exchange contract")
    def test_replace_file_restores_leaf_exchanged_inside_final_syscall(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            target = root / "target.txt"
            parked = root / "parked-original.txt"
            attacker = root / "attacker.txt"
            source.write_bytes(b"source\n")
            target.write_bytes(b"original\n")
            attacker.write_bytes(b"attacker\n")
            real_exchange = project_fs_module._posix_rename_exchange
            exchanged = False

            def exchange_leaf_then_publish(*args):
                nonlocal exchanged
                if not exchanged:
                    target.rename(parked)
                    attacker.rename(target)
                    exchanged = True
                return real_exchange(*args)

            with (
                project_fs(root) as fs,
                patch.object(
                    project_fs_module,
                    "_posix_rename_exchange",
                    side_effect=exchange_leaf_then_publish,
                ),
            ):
                with self.assertRaisesRegex(Exception, "path-identity-changed"):
                    fs.replace_file(Path("source.txt"), Path("target.txt"))

            self.assertTrue(exchanged)
            self.assertEqual(source.read_bytes(), b"source\n")
            self.assertEqual(target.read_bytes(), b"attacker\n")
            self.assertEqual(parked.read_bytes(), b"original\n")

    @unittest.skipIf(os.name == "nt", "POSIX atomic exchange contract")
    def test_replace_file_restores_source_raced_inside_final_syscall(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            target = root / "target.txt"
            parked = root / "parked-source.txt"
            attacker = root / "attacker.txt"
            source.write_bytes(b"source\n")
            target.write_bytes(b"original\n")
            attacker.write_bytes(b"attacker\n")
            real_exchange = project_fs_module._posix_rename_exchange
            exchanged = False

            def exchange_source_then_publish(*args):
                nonlocal exchanged
                if not exchanged:
                    source.rename(parked)
                    attacker.rename(source)
                    exchanged = True
                return real_exchange(*args)

            with (
                project_fs(root) as fs,
                patch.object(
                    project_fs_module,
                    "_posix_rename_exchange",
                    side_effect=exchange_source_then_publish,
                ),
            ):
                with self.assertRaisesRegex(Exception, "path-identity-changed"):
                    fs.replace_file(Path("source.txt"), Path("target.txt"))

            self.assertTrue(exchanged)
            self.assertEqual(source.read_bytes(), b"attacker\n")
            self.assertEqual(target.read_bytes(), b"original\n")
            self.assertEqual(parked.read_bytes(), b"source\n")

    def test_replace_file_rejects_stale_source_receipt_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            replacement = root / "replacement.txt"
            destination = root / "target.txt"
            source.write_bytes(b"verified-source\n")
            replacement.write_bytes(b"raced-source\n")
            destination.write_bytes(b"original-target\n")

            with project_fs(root) as fs:
                source_receipt = fs._snapshot(
                    Path("source.txt"),
                    allow_missing=False,
                )
                os.replace(replacement, source)
                with self.assertRaisesRegex(
                    Exception,
                    r"source\.txt: path-identity-changed",
                ):
                    fs.replace_file(
                        Path("source.txt"),
                        Path("target.txt"),
                        expected_source=source_receipt,
                    )

            self.assertEqual(source.read_bytes(), b"raced-source\n")
            self.assertEqual(destination.read_bytes(), b"original-target\n")

    @unittest.skipIf(os.name == "nt", "POSIX post-exchange cleanup contract")
    def test_replace_file_rejects_source_leaf_exchanged_during_cleanup(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            target = root / "target.txt"
            parked = root / "parked-original.txt"
            attacker = root / "attacker.txt"
            source.write_bytes(b"source\n")
            target.write_bytes(b"original\n")
            attacker.write_bytes(b"attacker\n")
            real_noreplace = project_fs_module._posix_rename_noreplace
            exchanged = False

            def exchange_source_then_quarantine(*args):
                nonlocal exchanged
                if not exchanged:
                    source.rename(parked)
                    attacker.rename(source)
                    exchanged = True
                return real_noreplace(*args)

            with (
                project_fs(root) as fs,
                patch.object(
                    project_fs_module,
                    "_posix_rename_noreplace",
                    side_effect=exchange_source_then_quarantine,
                ),
            ):
                with self.assertRaisesRegex(Exception, "path-identity-changed"):
                    fs.replace_file(Path("source.txt"), Path("target.txt"))

            self.assertTrue(exchanged)
            self.assertEqual(source.read_bytes(), b"source\n")
            self.assertEqual(target.read_bytes(), b"attacker\n")
            self.assertEqual(parked.read_bytes(), b"original\n")

    @unittest.skipIf(os.name == "nt", "POSIX mode bits contract")
    def test_posix_mode_000_cleanup_fails_before_canonical_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target.txt"
            source = root / "source.txt"

            with project_fs(root) as fs:
                target.write_bytes(b"delete-me\n")
                target.chmod(0)
                with self.assertRaisesRegex(Exception, "unsafe-target"):
                    fs.unlink_regular(Path("target.txt"))
                target.chmod(0o600)
                self.assertEqual(target.read_bytes(), b"delete-me\n")

                target.chmod(0)
                with self.assertRaisesRegex(Exception, "unsafe-target"):
                    fs.atomic_write(Path("target.txt"), b"atomic\n")
                target.chmod(0o600)
                self.assertEqual(target.read_bytes(), b"delete-me\n")

                source.write_bytes(b"source\n")
                target.chmod(0)
                with self.assertRaisesRegex(Exception, "unsafe-target"):
                    fs.replace_file(Path("source.txt"), Path("target.txt"))
                target.chmod(0o600)
                self.assertEqual(target.read_bytes(), b"delete-me\n")
                self.assertEqual(source.read_bytes(), b"source\n")

                source.chmod(0)
                with self.assertRaisesRegex(Exception, "unsafe-target"):
                    fs.replace_file(Path("source.txt"), Path("target.txt"))
                source.chmod(0o600)
                self.assertEqual(target.read_bytes(), b"delete-me\n")
                self.assertEqual(source.read_bytes(), b"source\n")

            self.assertEqual(tuple(root.glob(".*.kafa-delete-*.tmp")), ())
            self.assertEqual(tuple(root.glob(".*.kafa-*.tmp")), ())

    @unittest.skipIf(os.name == "nt", "POSIX cleanup rollback contract")
    def test_posix_delete_error_restores_each_canonical_operation(self) -> None:
        from core import project_fs as project_fs_module

        for operation in ("unlink", "atomic", "replace"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                target = root / "target.txt"
                source = root / "source.txt"
                target.write_bytes(b"original\n")
                if operation == "replace":
                    source.write_bytes(b"source\n")
                real_unlink = project_fs_module.os.unlink
                injected = False

                def reject_first_quarantine_delete(path, *args, **kwargs):
                    nonlocal injected
                    if ".kafa-delete-" in os.fspath(path) and not injected:
                        injected = True
                        raise PermissionError("injected quarantine delete failure")
                    return real_unlink(path, *args, **kwargs)

                with (
                    project_fs(root) as fs,
                    patch.object(
                        project_fs_module.os,
                        "unlink",
                        side_effect=reject_first_quarantine_delete,
                    ),
                ):
                    with self.assertRaisesRegex(Exception, "unsafe-target"):
                        if operation == "unlink":
                            fs.unlink_regular(Path("target.txt"))
                        elif operation == "atomic":
                            fs.atomic_write(Path("target.txt"), b"new\n")
                        else:
                            fs.replace_file(Path("source.txt"), Path("target.txt"))

                self.assertTrue(injected)
                self.assertEqual(target.read_bytes(), b"original\n")
                if operation == "replace":
                    self.assertEqual(source.read_bytes(), b"source\n")
                self.assertEqual(tuple(root.glob(".*.kafa-*.tmp")), ())

    @unittest.skipIf(os.name == "nt", "POSIX cancellation rollback contract")
    def test_posix_delete_cancellation_restores_canonical_names(self) -> None:
        from core import project_fs as project_fs_module

        for operation in ("unlink", "atomic", "replace", "rmdir"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                target = root / ("target-dir" if operation == "rmdir" else "target.txt")
                source = root / "source.txt"
                if operation == "rmdir":
                    target.mkdir()
                else:
                    target.write_bytes(b"original\n")
                if operation == "replace":
                    source.write_bytes(b"source\n")
                function_name = "rmdir" if operation == "rmdir" else "unlink"
                real_delete = getattr(project_fs_module.os, function_name)
                injected = False

                def cancel_first_quarantine_delete(path, *args, **kwargs):
                    nonlocal injected
                    if ".kafa-delete-" in os.fspath(path) and not injected:
                        injected = True
                        raise KeyboardInterrupt("injected delete cancellation")
                    return real_delete(path, *args, **kwargs)

                with (
                    project_fs(root) as fs,
                    patch.object(
                        project_fs_module.os,
                        function_name,
                        side_effect=cancel_first_quarantine_delete,
                    ),
                ):
                    with self.assertRaisesRegex(
                        KeyboardInterrupt,
                        "injected delete cancellation",
                    ):
                        if operation == "unlink":
                            fs.unlink_regular(Path("target.txt"))
                        elif operation == "atomic":
                            fs.atomic_write(Path("target.txt"), b"new\n")
                        elif operation == "replace":
                            fs.replace_file(Path("source.txt"), Path("target.txt"))
                        else:
                            fs.remove_empty_directory(Path("target-dir"))

                self.assertTrue(injected)
                if operation == "rmdir":
                    self.assertTrue(target.is_dir())
                else:
                    self.assertEqual(target.read_bytes(), b"original\n")
                if operation == "replace":
                    self.assertEqual(source.read_bytes(), b"source\n")
                self.assertEqual(tuple(root.glob(".*.kafa-*.tmp")), ())

    @unittest.skipIf(os.name == "nt", "POSIX quarantine cancellation contract")
    def test_posix_post_quarantine_cancellation_restores_canonical_name(self) -> None:
        from core import project_fs as project_fs_module

        for directory in (False, True):
            with self.subTest(directory=directory), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                target = root / ("target-dir" if directory else "target.txt")
                if directory:
                    target.mkdir()
                else:
                    target.write_bytes(b"original\n")
                real_snapshot = project_fs_module._PosixBackend._snapshot_at
                injected = False

                def cancel_first_quarantine_snapshot(
                    backend,
                    parent,
                    name,
                    relative,
                    **kwargs,
                ):
                    nonlocal injected
                    if ".kafa-delete-" in name and not injected:
                        injected = True
                        raise KeyboardInterrupt(
                            "injected post-quarantine cancellation"
                        )
                    return real_snapshot(
                        backend,
                        parent,
                        name,
                        relative,
                        **kwargs,
                    )

                with (
                    project_fs(root) as fs,
                    patch.object(
                        project_fs_module._PosixBackend,
                        "_snapshot_at",
                        new=cancel_first_quarantine_snapshot,
                    ),
                ):
                    with self.assertRaisesRegex(
                        KeyboardInterrupt,
                        "injected post-quarantine cancellation",
                    ):
                        if directory:
                            fs.remove_empty_directory(Path("target-dir"))
                        else:
                            fs.unlink_regular(Path("target.txt"))

                self.assertTrue(injected)
                if directory:
                    self.assertTrue(target.is_dir())
                else:
                    self.assertEqual(target.read_bytes(), b"original\n")
                self.assertEqual(tuple(root.glob(".*.kafa-delete-*.tmp")), ())

    @unittest.skipIf(os.name == "nt", "POSIX publication cancellation contract")
    def test_posix_post_rename_cancellation_restores_write_and_replace(self) -> None:
        from core import project_fs as project_fs_module

        for operation, existing in (
            ("atomic", True),
            ("replace", True),
            ("atomic", False),
            ("replace", False),
        ):
            with (
                self.subTest(operation=operation, existing=existing),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                source = root / "source.txt"
                target = root / "target.txt"
                if operation == "replace":
                    source.write_bytes(b"source\n")
                if existing:
                    target.write_bytes(b"original\n")

                primitive_name = (
                    "_posix_rename_exchange"
                    if existing
                    else "_posix_rename_noreplace"
                )
                real_rename = getattr(project_fs_module, primitive_name)
                calls = 0

                def rename_then_cancel(*args):
                    nonlocal calls
                    result = real_rename(*args)
                    calls += 1
                    if calls == 1:
                        raise KeyboardInterrupt(
                            "injected post-rename cancellation"
                        )
                    return result

                with (
                    project_fs(root) as fs,
                    patch.object(
                        project_fs_module,
                        primitive_name,
                        side_effect=rename_then_cancel,
                    ),
                ):
                    with self.assertRaisesRegex(
                        KeyboardInterrupt,
                        "injected post-rename cancellation",
                    ):
                        if operation == "atomic":
                            fs.atomic_write(Path("target.txt"), b"new\n")
                        else:
                            fs.replace_file(Path("source.txt"), Path("target.txt"))

                self.assertGreaterEqual(calls, 2)
                if existing:
                    self.assertEqual(target.read_bytes(), b"original\n")
                else:
                    self.assertFalse(target.exists())
                if operation == "replace":
                    self.assertEqual(source.read_bytes(), b"source\n")
                self.assertEqual(tuple(root.glob(".*.kafa-*.tmp")), ())

    @unittest.skipIf(os.name == "nt", "POSIX raced cancellation contract")
    def test_posix_rename_cancellation_restores_observed_raced_state(self) -> None:
        from core import project_fs as project_fs_module

        for existing in (True, False):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / "source.txt"
                target = root / "target.txt"
                attacker = root / "attacker.txt"
                parked = root / "parked-original.txt"
                source.write_bytes(b"source\n")
                attacker.write_bytes(b"attacker\n")
                if existing:
                    target.write_bytes(b"original\n")
                    primitive_name = "_posix_rename_exchange"
                else:
                    primitive_name = "_posix_rename_noreplace"
                real_rename = getattr(project_fs_module, primitive_name)
                calls = 0

                def race_then_cancel(*args):
                    nonlocal calls
                    if calls == 0:
                        if existing:
                            target.rename(parked)
                            attacker.rename(target)
                        else:
                            source.rename(parked)
                            attacker.rename(source)
                    result = real_rename(*args)
                    calls += 1
                    if calls == 1:
                        raise KeyboardInterrupt(
                            "injected raced rename cancellation"
                        )
                    return result

                with (
                    project_fs(root) as fs,
                    patch.object(
                        project_fs_module,
                        primitive_name,
                        side_effect=race_then_cancel,
                    ),
                ):
                    with self.assertRaisesRegex(
                        KeyboardInterrupt,
                        "injected raced rename cancellation",
                    ) as raised:
                        fs.replace_file(Path("source.txt"), Path("target.txt"))

                self.assertEqual(calls, 2)
                self.assertEqual(getattr(raised.exception, "__notes__", ()), ())
                if existing:
                    self.assertEqual(source.read_bytes(), b"source\n")
                    self.assertEqual(target.read_bytes(), b"attacker\n")
                    self.assertEqual(parked.read_bytes(), b"original\n")
                else:
                    self.assertEqual(source.read_bytes(), b"attacker\n")
                    self.assertFalse(target.exists())
                    self.assertEqual(parked.read_bytes(), b"source\n")

    @unittest.skipIf(os.name == "nt", "POSIX directory rollback contract")
    def test_nonempty_directory_delete_restores_canonical_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target-dir"
            target.mkdir()
            (target / "child.txt").write_bytes(b"child\n")

            with project_fs(root) as fs:
                with self.assertRaisesRegex(Exception, "unsafe-target"):
                    fs.remove_empty_directory(Path("target-dir"))

            self.assertEqual((target / "child.txt").read_bytes(), b"child\n")
            self.assertEqual(tuple(root.glob(".*.kafa-delete-*.tmp")), ())

    @unittest.skipIf(os.name == "nt", "POSIX create cleanup contract")
    def test_failed_create_does_not_delete_leaf_exchanged_during_cleanup(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target.txt"
            attacker = root / "attacker.txt"
            parked = root / "parked-created.txt"
            attacker.write_bytes(b"attacker\n")
            real_noreplace = project_fs_module._posix_rename_noreplace
            exchanged = False

            def fail_write(_backend, descriptor: int, data: bytes) -> None:
                os.write(descriptor, data[:1])
                raise OSError("injected create write failure")

            def exchange_created_then_quarantine(*args):
                nonlocal exchanged
                if not exchanged:
                    target.rename(parked)
                    attacker.rename(target)
                    exchanged = True
                return real_noreplace(*args)

            with (
                project_fs(root) as fs,
                patch.object(
                    project_fs_module._PosixBackend,
                    "_write_all",
                    new=fail_write,
                ),
                patch.object(
                    project_fs_module,
                    "_posix_rename_noreplace",
                    side_effect=exchange_created_then_quarantine,
                ),
            ):
                with self.assertRaisesRegex(OSError, "injected create write failure"):
                    fs.create_exclusive(Path("target.txt"), b"created\n")

            self.assertTrue(exchanged)
            self.assertEqual(target.read_bytes(), b"attacker\n")
            self.assertEqual(parked.read_bytes(), b"c")

    @unittest.skipIf(os.name == "nt", "POSIX exclusive rename contract")
    def test_unlink_restores_leaf_exchanged_inside_final_syscall(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target.txt"
            parked = root / "parked-original.txt"
            attacker = root / "attacker.txt"
            target.write_bytes(b"original\n")
            attacker.write_bytes(b"attacker\n")
            real_noreplace = project_fs_module._posix_rename_noreplace
            exchanged = False

            def exchange_leaf_then_quarantine(*args):
                nonlocal exchanged
                if not exchanged:
                    target.rename(parked)
                    attacker.rename(target)
                    exchanged = True
                return real_noreplace(*args)

            with (
                project_fs(root) as fs,
                patch.object(
                    project_fs_module,
                    "_posix_rename_noreplace",
                    side_effect=exchange_leaf_then_quarantine,
                ),
            ):
                with self.assertRaisesRegex(Exception, "path-identity-changed"):
                    fs.unlink_regular(Path("target.txt"))

            self.assertTrue(exchanged)
            self.assertEqual(target.read_bytes(), b"attacker\n")
            self.assertEqual(parked.read_bytes(), b"original\n")

    @unittest.skipIf(os.name == "nt", "POSIX exclusive rename contract")
    def test_rmdir_restores_leaf_exchanged_inside_final_syscall(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target-dir"
            parked = root / "parked-original-dir"
            attacker = root / "attacker-dir"
            target.mkdir()
            attacker.mkdir()
            real_noreplace = project_fs_module._posix_rename_noreplace
            exchanged = False

            def exchange_leaf_then_quarantine(*args):
                nonlocal exchanged
                if not exchanged:
                    target.rename(parked)
                    attacker.rename(target)
                    exchanged = True
                return real_noreplace(*args)

            with (
                project_fs(root) as fs,
                patch.object(
                    project_fs_module,
                    "_posix_rename_noreplace",
                    side_effect=exchange_leaf_then_quarantine,
                ),
            ):
                with self.assertRaisesRegex(Exception, "path-identity-changed"):
                    fs.remove_empty_directory(Path("target-dir"))

            self.assertTrue(exchanged)
            self.assertTrue(target.is_dir())
            self.assertTrue(parked.is_dir())

    @unittest.skipIf(os.name == "nt", "POSIX create-new lock contract")
    def test_open_lock_rejects_missing_leaf_created_inside_open(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "operation.lock"
            real_open = project_fs_module.os.open
            injected = False

            def create_leaf_then_open(path, flags, *args, **kwargs):
                nonlocal injected
                if path == "operation.lock" and flags & os.O_EXCL and not injected:
                    target.write_bytes(b"attacker-lock\n")
                    injected = True
                return real_open(path, flags, *args, **kwargs)

            with (
                project_fs(root) as fs,
                patch.object(
                    project_fs_module.os,
                    "open",
                    side_effect=create_leaf_then_open,
                ),
            ):
                with self.assertRaisesRegex(Exception, "path-identity-changed"):
                    fs.open_lock_fd(Path("operation.lock"))

            self.assertTrue(injected)
            self.assertEqual(target.read_bytes(), b"attacker-lock\n")

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

            with project_fs(alias) as fs:
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
            with project_fs(root) as fs:
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
                with project_fs(root) as fs:
                    fs.atomic_write(Path("target.txt"), b"after\n")
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
                with project_fs(root) as fs:
                    fs.atomic_write(Path("junction/result.txt"), b"unsafe")
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
            attacker_errors: list[OSError] = []
            attacker_done = threading.Event()

            def attack_source_path() -> None:
                try:
                    os.replace(source, parked)
                    source.write_bytes(b"substituted-staging\n")
                except OSError as exc:
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

            with project_fs(root) as fs:
                fs.replace_file(
                    Path("staging.db"),
                    Path("active.db"),
                )

            self.assertTrue(attacker_errors)
            self.assertIsInstance(attacker_errors[0], OSError)
            self.assertEqual(destination.read_bytes(), b"verified-staging\n")
            self.assertFalse(source.exists())
            self.assertFalse(parked.exists())

    @unittest.skipUnless(os.name == "nt", "Windows handle-publish contract")
    def test_windows_existing_destination_posix_rename_is_rechecked(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            destination = root / "destination.txt"
            attacker = root / "attacker.txt"
            source.write_bytes(b"source\n")
            destination.write_bytes(b"destination\n")
            attacker.write_bytes(b"attacker\n")
            original_hook = project_fs_module._before_windows_handle_rename
            attack_succeeded = False
            attack_errors: list[BaseException] = []

            def replace_destination_by_handle(
                backend,
                relative: Path,
                target: Path,
            ) -> None:
                nonlocal attack_succeeded
                if relative != Path("source.txt"):
                    return
                attacker_handle = backend._open_handle(
                    attacker,
                    directory=False,
                    access=(
                        project_fs_module._GENERIC_READ
                        | project_fs_module._GENERIC_WRITE
                        | project_fs_module._DELETE
                    ),
                )
                try:
                    backend._rename_by_handle(
                        attacker_handle,
                        root / target,
                        target,
                        replace_existing=True,
                    )
                    attack_succeeded = True
                except BaseException as exc:
                    attack_errors.append(exc)
                finally:
                    backend._close(attacker_handle)

            project_fs_module._before_windows_handle_rename = (
                replace_destination_by_handle
            )
            self.addCleanup(
                setattr,
                project_fs_module,
                "_before_windows_handle_rename",
                original_hook,
            )

            operation_error: BaseException | None = None
            try:
                with project_fs(root) as fs:
                    fs.replace_file(
                        Path("source.txt"),
                        Path("destination.txt"),
                    )
            except BaseException as exc:
                operation_error = exc

            if attack_succeeded:
                self.assertIsNotNone(operation_error)
                self.assertIn("path-identity-changed", str(operation_error))
                self.assertEqual(destination.read_bytes(), b"attacker\n")
                self.assertEqual(source.read_bytes(), b"source\n")
            else:
                self.assertTrue(attack_errors)
                self.assertIsNone(operation_error)
                self.assertEqual(destination.read_bytes(), b"source\n")

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
                with project_fs(root) as fs:
                    fs.atomic_write(
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
                with project_fs(root) as fs:
                    fs.atomic_write(
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
                with project_fs(root) as fs:
                    fs.unlink_regular(Path("retired.txt"))

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

            created: Path | None = None
            operation_error: BaseException | None = None
            try:
                with project_fs(root) as fs:
                    created = fs.create_unique_directory(
                        Path("backups"),
                        "schema-",
                    )
            except BaseException as exc:
                operation_error = exc

            if attacker_errors:
                self.assertIsInstance(attacker_errors[0], PermissionError)
                self.assertIn(
                    getattr(attacker_errors[0], "winerror", None),
                    {5, 32},
                )
                self.assertIsNone(operation_error)
                self.assertIsNotNone(created)
                assert created is not None
                self.assertTrue((root / created).is_dir())
                self.assertFalse(parked.exists())
            else:
                self.assertIsNotNone(operation_error)
                self.assertIn("path-identity-changed", str(operation_error))
                self.assertFalse(parent.exists())
                self.assertTrue(parked.is_dir())
                self.assertEqual(tuple(parked.iterdir()), ())

    @unittest.skipUnless(os.name == "nt", "Windows final-leaf pin contract")
    def test_windows_final_leaf_blocks_move_and_write_through_backup_cleanup(
        self,
    ) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            target = root / "target.txt"
            parked = base / "parked-target.txt"
            blocked_moves: list[OSError] = []
            blocked_writes: list[OSError] = []
            original_hook = project_fs_module._before_windows_backup_cleanup

            def require_sharing_denial(
                operation,
                failures: list[OSError],
                *,
                allow_crt_eacces: bool = False,
            ) -> None:
                try:
                    operation()
                except OSError as exc:
                    native_sharing_denial = getattr(
                        exc,
                        "winerror",
                        None,
                    ) in {5, 32}
                    crt_sharing_denial = (
                        allow_crt_eacces
                        and getattr(exc, "winerror", None) is None
                        and exc.errno == errno.EACCES
                    )
                    if not isinstance(exc, PermissionError) or not (
                        native_sharing_denial or crt_sharing_denial
                    ):
                        raise
                    failures.append(exc)
                else:
                    self.fail("attacker operation unexpectedly succeeded")

            def attempt_final_move(
                _backend,
                destination: Path,
                _backup: Path,
            ) -> None:
                if destination != Path("target.txt"):
                    return
                require_sharing_denial(
                    lambda: target.rename(parked),
                    blocked_moves,
                )
                require_sharing_denial(
                    lambda: target.write_bytes(b"attacker\n"),
                    blocked_writes,
                    allow_crt_eacces=True,
                )

            project_fs_module._before_windows_backup_cleanup = attempt_final_move
            self.addCleanup(
                setattr,
                project_fs_module,
                "_before_windows_backup_cleanup",
                original_hook,
            )

            with project_fs(root) as fs:
                fs.atomic_write(Path("target.txt"), b"old\n", mode=0o600)
                fs.atomic_write(Path("target.txt"), b"new\n", mode=0o600)

            self.assertEqual(len(blocked_moves), 1)
            self.assertEqual(len(blocked_writes), 1)
            self.assertEqual(target.read_bytes(), b"new\n")
            self.assertFalse(parked.exists())
            target.write_bytes(b"after-close\n")
            target.rename(parked)
            parked.rename(target)
            self.assertEqual(target.read_bytes(), b"after-close\n")

    @unittest.skipUnless(os.name == "nt", "Windows final-leaf hardlink contract")
    def test_windows_final_leaf_hardlink_race_rolls_back_and_fails_closed(
        self,
    ) -> None:
        from core import project_fs as project_fs_module
        from core.project_fs import ProjectPathSafetyError

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            target = root / "target.txt"
            outside_alias = base / "target-alias.txt"
            original_hook = project_fs_module._before_windows_backup_cleanup

            def hardlink_final_leaf(
                _backend,
                destination: Path,
                _backup: Path,
            ) -> None:
                if destination == Path("target.txt"):
                    os.link(target, outside_alias)

            project_fs_module._before_windows_backup_cleanup = hardlink_final_leaf
            self.addCleanup(
                setattr,
                project_fs_module,
                "_before_windows_backup_cleanup",
                original_hook,
            )
            self.addCleanup(outside_alias.unlink, missing_ok=True)

            with project_fs(root) as fs:
                fs.atomic_write(Path("target.txt"), b"old\n", mode=0o600)
                with self.assertRaisesRegex(
                    ProjectPathSafetyError,
                    "hard-linked-target",
                ):
                    fs.atomic_write(Path("target.txt"), b"new\n", mode=0o600)

            self.assertEqual(target.read_bytes(), b"old\n")
            self.assertEqual(outside_alias.read_bytes(), b"new\n")
            parked = base / "parked-target.txt"
            target.write_bytes(b"after-close\n")
            target.rename(parked)
            parked.rename(target)
            self.assertEqual(target.read_bytes(), b"after-close\n")


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
            source_mode = stat.S_IMODE(
                (root / "state/source.txt").stat().st_mode
            )
            if os.name == "nt":
                self.assertTrue(source_mode & stat.S_IWUSR)
            else:
                self.assertEqual(source_mode, 0o640)

    def test_unique_directory_rejects_prefixes_that_form_unsafe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with project_fs(root) as fs:
                for prefix in ("unsafe:", "CON.", "control\x01", "slash/", "backslash\\"):
                    with self.subTest(prefix=prefix), self.assertRaisesRegex(
                        Exception,
                        "invalid-relative-path",
                    ):
                        fs.create_unique_directory(Path("backups"), prefix)

            self.assertFalse((root / "backups").exists())

    def test_existing_operation_lock_mode_is_normalized_without_identity_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lock = root / ".ai-team/state/harness.db.operation.lock"
            lock.parent.mkdir(parents=True)
            lock.write_bytes(b"\0")
            lock.chmod(0o644)

            with project_db_operation(root):
                self.assertTrue(lock.is_file())

            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

    def test_atomic_write_preserves_readonly_and_writable_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "state/mode.txt"
            with project_fs(root) as fs:
                fs.atomic_write(Path("state/mode.txt"), b"readonly\n", mode=0o444)
                readonly_mode = stat.S_IMODE(target.stat().st_mode)
                self.assertFalse(readonly_mode & stat.S_IWUSR)
                fs.atomic_write(Path("state/mode.txt"), b"writable\n", mode=0o600)
                writable_mode = stat.S_IMODE(target.stat().st_mode)
                self.assertTrue(writable_mode & stat.S_IWUSR)
                fs.atomic_write(
                    Path("state/mode.txt"),
                    b"readonly-again\n",
                    mode=0o444,
                )
                readonly_again_mode = stat.S_IMODE(target.stat().st_mode)
                self.assertFalse(readonly_again_mode & stat.S_IWUSR)

    @unittest.skipUnless(os.name == "nt", "Windows readonly delete contract")
    def test_windows_unlink_regular_removes_readonly_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "state/readonly.txt"
            with project_fs(root) as fs:
                fs.atomic_write(
                    Path("state/readonly.txt"),
                    b"readonly\n",
                    mode=0o444,
                )
                self.assertTrue(target.exists())
                fs.unlink_regular(Path("state/readonly.txt"))
            self.assertFalse(target.exists())

    @unittest.skipUnless(os.name == "nt", "Windows readonly cleanup contract")
    def test_windows_interrupted_readonly_atomic_write_cleans_temporary(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original_hook = project_fs_module._before_atomic_replace

            def interrupt(_fs, _relative: Path) -> None:
                raise KeyboardInterrupt("injected-readonly-publication-failure")

            project_fs_module._before_atomic_replace = interrupt
            self.addCleanup(
                setattr,
                project_fs_module,
                "_before_atomic_replace",
                original_hook,
            )
            with project_fs(root) as fs:
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "injected-readonly-publication-failure",
                ):
                    fs.atomic_write(
                        Path("readonly.txt"),
                        b"readonly\n",
                        mode=0o444,
                    )
            self.assertEqual(tuple(root.glob(".readonly.txt.kafa-*.tmp")), ())

    def test_bounded_audit_rejects_unbounded_inventory(self) -> None:
        from core.project_fs import MAX_AUDIT_PATHS

        with tempfile.TemporaryDirectory() as temp:
            with project_fs(Path(temp)) as fs:
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

    def test_windows_replacefile_documented_failure_states_are_reconciled(self) -> None:
        from core import project_fs as project_fs_module
        from core.project_fs import (
            ProjectPathSafetyError,
            _PathIdentity,
            _PathSnapshot,
            _WindowsBackend,
        )

        source = Path("source.txt")
        destination = Path("target.txt")
        source_identity = _PathIdentity(
            volume=1,
            file_id=b"source",
            kind="file",
            mode_or_attributes=0,
            nlink=1,
        )
        destination_identity = _PathIdentity(
            volume=1,
            file_id=b"destination",
            kind="file",
            mode_or_attributes=0,
            nlink=1,
        )
        source_snapshot = _PathSnapshot(True, source_identity)
        destination_snapshot = _PathSnapshot(True, destination_identity)
        missing = _PathSnapshot(False, None)

        for error_code in (50, 1175, 1176, 1177):
            with self.subTest(error_code=error_code), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                backup = Path(
                    ".target.txt.kafa-displaced-aaaaaaaaaaaaaaaaaaaaaaaa.tmp"
                )
                state = {
                    source: source_snapshot,
                    destination: destination_snapshot,
                    backup: missing,
                }
                moves: list[tuple[Path, Path]] = []
                closed_handles: list[int] = []
                opened_handles: list[dict[str, object]] = []

                class FakeApi:
                    def ReplaceFileW(
                        self,
                        _destination,
                        _source,
                        _backup,
                        _flags,
                        _exclude,
                        _reserved,
                    ) -> bool:
                        if error_code == 1177:
                            state[destination] = missing
                            state[backup] = destination_snapshot
                        return False

                    @staticmethod
                    def error() -> OSError:
                        return OSError(error_code, "injected ReplaceFileW failure")

                    @staticmethod
                    def FlushFileBuffers(_handle) -> bool:
                        return True

                backend = object.__new__(_WindowsBackend)
                backend.root = root
                backend.api = FakeApi()

                def snapshot(relative: Path, *, allow_missing: bool, **_kwargs):
                    result = state.get(Path(relative), missing)
                    if not allow_missing and not result.exists:
                        raise AssertionError(f"unexpected missing path: {relative}")
                    return result

                def replace_file(
                    move_source: Path,
                    move_destination: Path,
                    expected_destination=None,
                ) -> None:
                    move_source = Path(move_source)
                    move_destination = Path(move_destination)
                    self.assertEqual(state[move_destination], expected_destination)
                    self.assertTrue(state[move_source].exists)
                    moves.append((move_source, move_destination))
                    state[move_destination] = state[move_source]
                    state[move_source] = missing

                def unlink_regular(relative: Path, *, expected, **_kwargs) -> None:
                    relative = Path(relative)
                    if error_code == 1177:
                        self.assertNotIn(101, closed_handles)
                    self.assertEqual(state[relative], expected)
                    state[relative] = missing

                backend.snapshot = snapshot
                backend._raw_snapshot = snapshot
                backend.replace_file = replace_file

                def open_handle(*_args, **kwargs):
                    opened_handles.append(dict(kwargs))
                    return 101

                backend._open_handle = open_handle
                backend._identity = lambda *_args, **_kwargs: source_identity
                backend._close = lambda handle: closed_handles.append(int(handle))
                backend._apply_file_attributes = lambda *_args, **_kwargs: None
                backend.unlink_regular = unlink_regular

                with (
                    patch.object(
                        project_fs_module.secrets,
                        "token_hex",
                        return_value="a" * 24,
                    ),
                    patch.multiple(
                        project_fs_module,
                        _INVALID_HANDLE_VALUE=-1,
                        _GENERIC_READ=0x80000000,
                        _GENERIC_WRITE=0x40000000,
                        _REPLACEFILE_FLAGS_NONE=0,
                        create=True,
                    ),
                ):
                    if error_code in (50, 1175, 1176):
                        expected_reason = (
                            "platform-safety-unavailable"
                            if error_code == 50
                            else "path-identity-changed"
                        )
                        with self.assertRaisesRegex(
                            Exception,
                            expected_reason,
                        ) as raised:
                            backend._replace_with_backup_checked(
                                root / source,
                                source,
                                source_identity,
                                root / destination,
                                destination,
                                destination_snapshot,
                            )
                        self.assertIn(
                            "complete source metadata rollback is not verified",
                            "\n".join(
                                getattr(raised.exception, "__notes__", ())
                            ),
                        )
                        self.assertEqual(moves, [])
                    else:
                        result = backend._replace_with_backup_checked(
                            root / source,
                            source,
                            source_identity,
                            root / destination,
                            destination,
                            destination_snapshot,
                        )
                        self.assertEqual(result, source_identity)
                        self.assertEqual(moves, [(source, destination)])
                        self.assertEqual(closed_handles, [101])
                        self.assertTrue(
                            any(
                                opened.get("exclusive_share") is True
                                for opened in opened_handles
                            ),
                            opened_handles,
                        )

                if error_code in (50, 1175, 1176):
                    self.assertEqual(state[source], source_snapshot)
                    self.assertEqual(state[destination], destination_snapshot)
                    self.assertEqual(state[backup], missing)
                else:
                    self.assertEqual(state[source], missing)
                    self.assertEqual(state[destination], source_snapshot)
                    self.assertEqual(state[backup], missing)

    def test_windows_1177_completion_failure_restores_known_source_attributes_and_warns(
        self,
    ) -> None:
        from core import project_fs as project_fs_module
        from core.project_fs import _PathIdentity, _PathSnapshot, _WindowsBackend

        root = Path("C:/project")
        source = Path("source.txt")
        destination = Path("target.txt")
        backup = Path(
            ".target.txt.kafa-displaced-aaaaaaaaaaaaaaaaaaaaaaaa.tmp"
        )
        source_identity = _PathIdentity(1, b"source", "file", 0x01, 1)
        mutated_source_identity = _PathIdentity(
            1,
            b"source",
            "file",
            0x80,
            1,
        )
        destination_identity = _PathIdentity(
            1,
            b"destination",
            "file",
            0x80,
            1,
        )
        source_snapshot = _PathSnapshot(True, source_identity)
        destination_snapshot = _PathSnapshot(True, destination_identity)
        missing = _PathSnapshot(False)
        state = {
            source: source_snapshot,
            destination: destination_snapshot,
            backup: missing,
        }

        class FakeApi:
            calls = 0

            def ReplaceFileW(self, *_args) -> bool:
                self.calls += 1
                if self.calls == 1:
                    state[source] = _PathSnapshot(True, mutated_source_identity)
                    state[destination] = missing
                    state[backup] = destination_snapshot
                    return False
                state[source] = state[destination]
                state[destination] = state[backup]
                state[backup] = missing
                return True

            @staticmethod
            def error() -> OSError:
                return OSError(1177, "injected ERROR_UNABLE_TO_MOVE_REPLACEMENT_2")

        backend = object.__new__(_WindowsBackend)
        backend.root = root
        backend.api = FakeApi()

        def snapshot(relative: Path, *, allow_missing: bool, **_kwargs):
            result = state.get(Path(relative), missing)
            if not allow_missing and not result.exists:
                raise AssertionError(f"unexpected missing path: {relative}")
            return result

        completion_failure: BaseException = RuntimeError(
            "injected partial completion failure"
        )
        completion_commits_before_failure = False

        def replace_file(
            move_source: Path,
            move_destination: Path,
            expected_destination=None,
        ) -> None:
            move_source = Path(move_source)
            move_destination = Path(move_destination)
            self.assertEqual(state[move_destination], expected_destination)
            if move_source == source:
                if completion_commits_before_failure:
                    state[move_destination] = state[move_source]
                    state[move_source] = missing
                raise completion_failure
            self.assertEqual(move_source, backup)
            state[move_destination] = state[move_source]
            state[move_source] = missing

        def apply_file_attributes(_handle, relative: Path, attributes: int) -> None:
            self.assertEqual(Path(relative), source)
            current = state[source].identity
            assert current is not None
            state[source] = _PathSnapshot(
                True,
                _PathIdentity(
                    current.volume,
                    current.file_id,
                    current.kind,
                    int(attributes) or 0x80,
                    current.nlink,
                ),
            )

        backend.snapshot = snapshot
        backend._raw_snapshot = snapshot
        backend.replace_file = replace_file
        backend._open_handle = lambda *_args, **_kwargs: 101
        backend._identity = (
            lambda *_args, **_kwargs: state[source].identity
        )
        backend._raw_identity = backend._identity
        backend._close = lambda _handle: None
        backend._apply_file_attributes = apply_file_attributes

        with (
            patch.object(
                project_fs_module.secrets,
                "token_hex",
                return_value="a" * 24,
            ),
            patch.multiple(
                project_fs_module,
                _FILE_WRITE_ATTRIBUTES=0x0100,
                _REPLACEFILE_FLAGS_NONE=0,
                create=True,
            ),
        ):
            with self.assertRaisesRegex(
                Exception,
                "path-identity-changed",
            ) as raised:
                backend._replace_with_backup_checked(
                    root / source,
                    source,
                    source_identity,
                    root / destination,
                    destination,
                    destination_snapshot,
                )

        self.assertEqual(state[source], source_snapshot)
        self.assertEqual(state[destination], destination_snapshot)
        self.assertEqual(state[backup], missing)
        notes = "\n".join(getattr(raised.exception, "__notes__", ()))
        self.assertIn("partial replacement completion failed", notes)
        self.assertIn("complete source metadata rollback is not verified", notes)
        self.assertIn("requires manual review", notes)

        cancellation = KeyboardInterrupt(
            "injected partial completion cancellation"
        )
        backend.api.calls = 0
        completion_failure = cancellation
        with (
            patch.object(
                project_fs_module.secrets,
                "token_hex",
                return_value="a" * 24,
            ),
            patch.multiple(
                project_fs_module,
                _FILE_WRITE_ATTRIBUTES=0x0100,
                _REPLACEFILE_FLAGS_NONE=0,
                create=True,
            ),
            self.assertRaises(KeyboardInterrupt) as cancelled,
        ):
            backend._replace_with_backup_checked(
                root / source,
                source,
                source_identity,
                root / destination,
                destination,
                destination_snapshot,
            )

        self.assertIs(cancelled.exception, cancellation)
        self.assertEqual(state[source], source_snapshot)
        self.assertEqual(state[destination], destination_snapshot)
        self.assertEqual(state[backup], missing)
        cancellation_notes = "\n".join(
            getattr(cancellation, "__notes__", ())
        )
        self.assertIn("partial replacement completion failed", cancellation_notes)
        self.assertIn(
            "complete source metadata rollback is not verified",
            cancellation_notes,
        )
        self.assertIn("requires manual review", cancellation_notes)

        after_effect_cancellation = KeyboardInterrupt(
            "injected cancellation after partial completion committed"
        )
        completion_failure = after_effect_cancellation
        completion_commits_before_failure = True
        backend.api.calls = 0
        with (
            patch.object(
                project_fs_module.secrets,
                "token_hex",
                return_value="a" * 24,
            ),
            patch.multiple(
                project_fs_module,
                _FILE_WRITE_ATTRIBUTES=0x0100,
                _REPLACEFILE_FLAGS_NONE=0,
                create=True,
            ),
            self.assertRaises(KeyboardInterrupt) as after_effect,
        ):
            backend._replace_with_backup_checked(
                root / source,
                source,
                source_identity,
                root / destination,
                destination,
                destination_snapshot,
            )

        self.assertIs(after_effect.exception, after_effect_cancellation)
        self.assertEqual(backend.api.calls, 2)
        self.assertEqual(state[source], source_snapshot)
        self.assertEqual(state[destination], destination_snapshot)
        self.assertEqual(state[backup], missing)
        after_effect_notes = "\n".join(
            getattr(after_effect_cancellation, "__notes__", ())
        )
        self.assertIn("partial replacement completion failed", after_effect_notes)
        self.assertIn("requires manual review", after_effect_notes)

    def test_windows_replace_error_retrieval_cancellation_reconciles_partial_state(
        self,
    ) -> None:
        from core import project_fs as project_fs_module
        from core.project_fs import _PathIdentity, _PathSnapshot, _WindowsBackend

        root = Path("C:/project")
        source = Path("source.txt")
        destination = Path("target.txt")
        backup = Path(
            ".target.txt.kafa-displaced-aaaaaaaaaaaaaaaaaaaaaaaa.tmp"
        )
        source_identity = _PathIdentity(1, b"source", "file", 0x01, 1)
        mutated_source = _PathSnapshot(
            True,
            _PathIdentity(1, b"source", "file", 0x80, 1),
        )
        source_snapshot = _PathSnapshot(True, source_identity)
        destination_snapshot = _PathSnapshot(
            True,
            _PathIdentity(1, b"destination", "file", 0x80, 1),
        )
        missing = _PathSnapshot(False)
        state = {
            source: source_snapshot,
            destination: destination_snapshot,
            backup: missing,
        }
        cancellation = KeyboardInterrupt("cancel during GetLastError")
        restored_attributes: list[int] = []

        class FakeApi:
            @staticmethod
            def ReplaceFileW(*_args) -> bool:
                state[source] = mutated_source
                state[destination] = missing
                state[backup] = destination_snapshot
                return False

            @staticmethod
            def error() -> OSError:
                raise cancellation

        backend = object.__new__(_WindowsBackend)
        backend.root = root
        backend.api = FakeApi()

        def snapshot(relative: Path, *, allow_missing: bool, **_kwargs):
            result = state.get(Path(relative), missing)
            if not allow_missing and not result.exists:
                raise AssertionError(f"unexpected missing path: {relative}")
            return result

        def replace_file(move_source: Path, move_destination: Path, **_kwargs) -> None:
            move_source = Path(move_source)
            move_destination = Path(move_destination)
            state[move_destination] = state[move_source]
            state[move_source] = missing

        def restore_attributes(*_args) -> None:
            restored_attributes.append(source_identity.mode_or_attributes)
            state[source] = source_snapshot

        backend.snapshot = snapshot
        backend._raw_snapshot = snapshot
        backend.replace_file = replace_file
        backend._restore_known_file_attributes = restore_attributes
        backend._prepare_replacement_source = lambda *_args: None

        with (
            patch.object(
                project_fs_module.secrets,
                "token_hex",
                return_value="a" * 24,
            ),
            patch.multiple(
                project_fs_module,
                _REPLACEFILE_FLAGS_NONE=0,
                create=True,
            ),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            backend._replace_with_backup_checked(
                root / source,
                source,
                source_identity,
                root / destination,
                destination,
                destination_snapshot,
            )

        self.assertIs(raised.exception, cancellation)
        self.assertEqual(state[source], source_snapshot)
        self.assertEqual(state[destination], destination_snapshot)
        self.assertEqual(state[backup], missing)
        self.assertEqual(restored_attributes, [0x01])
        self.assertIn(
            "requires manual review",
            "\n".join(getattr(cancellation, "__notes__", ())),
        )

    def test_windows_replace_state_inspection_cancellation_preserves_cancellation(
        self,
    ) -> None:
        from core import project_fs as project_fs_module
        from core.project_fs import _PathIdentity, _PathSnapshot, _WindowsBackend

        root = Path("C:/project")
        source = Path("source.txt")
        destination = Path("target.txt")
        backup = Path(
            ".target.txt.kafa-displaced-aaaaaaaaaaaaaaaaaaaaaaaa.tmp"
        )
        source_identity = _PathIdentity(1, b"source", "file", 0x01, 1)
        source_snapshot = _PathSnapshot(True, source_identity)
        destination_snapshot = _PathSnapshot(
            True,
            _PathIdentity(1, b"destination", "file", 0x80, 1),
        )
        missing = _PathSnapshot(False)
        state = {
            source: source_snapshot,
            destination: destination_snapshot,
            backup: missing,
        }
        cancellation = KeyboardInterrupt("cancel during state inspection")
        inspect_calls = 0
        restored_attributes: list[int] = []

        class FakeApi:
            calls = 0

            def ReplaceFileW(self, *_args) -> bool:
                self.calls += 1
                if self.calls == 1:
                    state[source] = missing
                    state[destination] = source_snapshot
                    state[backup] = destination_snapshot
                else:
                    state[source] = state[destination]
                    state[destination] = state[backup]
                    state[backup] = missing
                return True

        backend = object.__new__(_WindowsBackend)
        backend.root = root
        backend.api = FakeApi()

        def snapshot(relative: Path, *, allow_missing: bool, **_kwargs):
            result = state.get(Path(relative), missing)
            if not allow_missing and not result.exists:
                raise AssertionError(f"unexpected missing path: {relative}")
            return result

        def raw_snapshot(relative: Path, *, allow_missing: bool, **_kwargs):
            nonlocal inspect_calls
            inspect_calls += 1
            if inspect_calls == 1:
                raise cancellation
            return snapshot(relative, allow_missing=allow_missing)

        def restore_attributes(*_args) -> None:
            restored_attributes.append(source_identity.mode_or_attributes)

        backend.snapshot = snapshot
        backend._raw_snapshot = raw_snapshot
        backend._restore_known_file_attributes = restore_attributes
        backend._prepare_replacement_source = lambda *_args: None

        with (
            patch.object(
                project_fs_module.secrets,
                "token_hex",
                return_value="a" * 24,
            ),
            patch.multiple(
                project_fs_module,
                _REPLACEFILE_FLAGS_NONE=0,
                create=True,
            ),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            backend._replace_with_backup_checked(
                root / source,
                source,
                source_identity,
                root / destination,
                destination,
                destination_snapshot,
            )

        self.assertIs(raised.exception, cancellation)
        self.assertEqual(backend.api.calls, 2)
        self.assertEqual(state[source], source_snapshot)
        self.assertEqual(state[destination], destination_snapshot)
        self.assertEqual(state[backup], missing)
        self.assertEqual(restored_attributes, [0x01])
        self.assertIn(
            "requires manual review",
            "\n".join(getattr(cancellation, "__notes__", ())),
        )

    def test_windows_1177_post_completion_inspection_cancellation_rolls_back(
        self,
    ) -> None:
        from core import project_fs as project_fs_module
        from core.project_fs import _PathIdentity, _PathSnapshot, _WindowsBackend

        root = Path("C:/project")
        source = Path("source.txt")
        destination = Path("target.txt")
        backup = Path(
            ".target.txt.kafa-displaced-aaaaaaaaaaaaaaaaaaaaaaaa.tmp"
        )
        source_identity = _PathIdentity(1, b"source", "file", 0x01, 1)
        source_snapshot = _PathSnapshot(True, source_identity)
        destination_snapshot = _PathSnapshot(
            True,
            _PathIdentity(1, b"destination", "file", 0x80, 1),
        )
        missing = _PathSnapshot(False)
        state = {
            source: source_snapshot,
            destination: destination_snapshot,
            backup: missing,
        }
        cancellation = KeyboardInterrupt(
            "cancel after partial replacement completion"
        )
        raw_calls = 0
        restored_attributes: list[int] = []

        class FakeApi:
            calls = 0

            def ReplaceFileW(self, *_args) -> bool:
                self.calls += 1
                if self.calls == 1:
                    state[destination] = missing
                    state[backup] = destination_snapshot
                    return False
                state[source] = state[destination]
                state[destination] = state[backup]
                state[backup] = missing
                return True

            @staticmethod
            def error() -> OSError:
                return OSError(
                    1177,
                    "injected ERROR_UNABLE_TO_MOVE_REPLACEMENT_2",
                )

        backend = object.__new__(_WindowsBackend)
        backend.root = root
        backend.api = FakeApi()

        def snapshot(relative: Path, *, allow_missing: bool, **_kwargs):
            result = state.get(Path(relative), missing)
            if not allow_missing and not result.exists:
                raise AssertionError(f"unexpected missing path: {relative}")
            return result

        def raw_snapshot(relative: Path, *, allow_missing: bool, **_kwargs):
            nonlocal raw_calls
            raw_calls += 1
            if raw_calls == 4:
                raise cancellation
            return snapshot(relative, allow_missing=allow_missing)

        def replace_file(move_source: Path, move_destination: Path, **_kwargs) -> None:
            move_source = Path(move_source)
            move_destination = Path(move_destination)
            state[move_destination] = state[move_source]
            state[move_source] = missing

        def restore_attributes(*_args) -> None:
            restored_attributes.append(source_identity.mode_or_attributes)

        backend.snapshot = snapshot
        backend._raw_snapshot = raw_snapshot
        backend.replace_file = replace_file
        backend._restore_known_file_attributes = restore_attributes
        backend._prepare_replacement_source = lambda *_args: None

        with (
            patch.object(
                project_fs_module.secrets,
                "token_hex",
                return_value="a" * 24,
            ),
            patch.multiple(
                project_fs_module,
                _REPLACEFILE_FLAGS_NONE=0,
                create=True,
            ),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            backend._replace_with_backup_checked(
                root / source,
                source,
                source_identity,
                root / destination,
                destination,
                destination_snapshot,
            )

        self.assertIs(raised.exception, cancellation)
        self.assertEqual(backend.api.calls, 2)
        self.assertEqual(state[source], source_snapshot)
        self.assertEqual(state[destination], destination_snapshot)
        self.assertEqual(state[backup], missing)
        self.assertEqual(restored_attributes, [0x01])
        self.assertIn(
            "requires manual review",
            "\n".join(getattr(cancellation, "__notes__", ())),
        )

    def test_windows_replacefile_restores_raced_destination_and_source(self) -> None:
        from core import project_fs as project_fs_module
        from core.project_fs import _PathIdentity, _PathSnapshot, _WindowsBackend

        root = Path("C:/project")
        source = Path("source.txt")
        destination = Path("target.txt")
        backup = Path(
            ".target.txt.kafa-displaced-aaaaaaaaaaaaaaaaaaaaaaaa.tmp"
        )
        missing = _PathSnapshot(False, None)

        def present(file_id: bytes) -> _PathSnapshot:
            return _PathSnapshot(
                True,
                _PathIdentity(
                    volume=1,
                    file_id=file_id,
                    kind="file",
                    mode_or_attributes=0,
                    nlink=1,
                ),
            )

        source_snapshot = present(b"source")
        destination_snapshot = present(b"expected-destination")
        attacker_snapshot = present(b"raced-destination")
        state = {
            source: source_snapshot,
            destination: destination_snapshot,
            backup: missing,
        }
        parked: list[_PathSnapshot] = []

        class FakeApi:
            calls = 0

            def ReplaceFileW(self, *_args) -> bool:
                self.calls += 1
                if self.calls == 1:
                    parked.append(state[destination])
                    state[destination] = source_snapshot
                    state[source] = missing
                    state[backup] = attacker_snapshot
                else:
                    state[source] = state[destination]
                    state[destination] = state[backup]
                    state[backup] = missing
                return True

            @staticmethod
            def error() -> OSError:
                return OSError(5, "unexpected fake API failure")

        backend = object.__new__(_WindowsBackend)
        backend.root = root
        backend.api = FakeApi()

        def snapshot(relative: Path, *, allow_missing: bool, **_kwargs):
            result = state.get(Path(relative), missing)
            if not allow_missing and not result.exists:
                raise AssertionError(f"unexpected missing path: {relative}")
            return result

        backend.snapshot = snapshot
        backend._raw_snapshot = snapshot

        with (
            patch.object(
                project_fs_module.secrets,
                "token_hex",
                return_value="a" * 24,
            ),
            patch.multiple(
                project_fs_module,
                _REPLACEFILE_FLAGS_NONE=0,
                create=True,
            ),
        ):
            with self.assertRaisesRegex(Exception, "path-identity-changed"):
                backend._replace_with_backup_checked(
                    root / source,
                    source,
                    source_snapshot.identity,
                    root / destination,
                    destination,
                    destination_snapshot,
                )

        self.assertEqual(backend.api.calls, 2)
        self.assertEqual(parked, [destination_snapshot])
        self.assertEqual(state[source], source_snapshot)
        self.assertEqual(state[destination], attacker_snapshot)
        self.assertEqual(state[backup], missing)

    def test_windows_replacefile_restores_unsafe_raced_destination(self) -> None:
        from core import project_fs as project_fs_module
        from core.project_fs import (
            ProjectPathSafetyError,
            _PathIdentity,
            _PathSnapshot,
            _WindowsBackend,
        )

        root = Path("C:/project")
        source = Path("source.txt")
        destination = Path("target.txt")
        backup = Path(
            ".target.txt.kafa-displaced-aaaaaaaaaaaaaaaaaaaaaaaa.tmp"
        )
        missing = _PathSnapshot(False, None)

        def present(file_id: bytes, *, nlink: int = 1) -> _PathSnapshot:
            return _PathSnapshot(
                True,
                _PathIdentity(
                    volume=1,
                    file_id=file_id,
                    kind="file",
                    mode_or_attributes=0,
                    nlink=nlink,
                ),
            )

        source_snapshot = present(b"source")
        destination_snapshot = present(b"expected-destination")
        attacker_snapshot = present(b"raced-hardlink", nlink=2)
        state = {
            source: source_snapshot,
            destination: destination_snapshot,
            backup: missing,
        }
        parked: list[_PathSnapshot] = []

        class FakeApi:
            calls = 0

            def ReplaceFileW(self, *_args) -> bool:
                self.calls += 1
                if self.calls == 1:
                    parked.append(state[destination])
                    state[destination] = source_snapshot
                    state[source] = missing
                    state[backup] = attacker_snapshot
                else:
                    state[source] = state[destination]
                    state[destination] = state[backup]
                    state[backup] = missing
                return True

            @staticmethod
            def error() -> OSError:
                return OSError(5, "unexpected fake API failure")

        backend = object.__new__(_WindowsBackend)
        backend.root = root
        backend.api = FakeApi()

        def raw_snapshot(relative: Path, *, allow_missing: bool, **_kwargs):
            result = state.get(Path(relative), missing)
            if not allow_missing and not result.exists:
                raise AssertionError(f"unexpected missing path: {relative}")
            return result

        def snapshot(relative: Path, *, allow_missing: bool, **kwargs):
            result = raw_snapshot(
                relative,
                allow_missing=allow_missing,
                **kwargs,
            )
            if (
                result.exists
                and result.identity is not None
                and result.identity.nlink != 1
            ):
                raise ProjectPathSafetyError(relative, "hard-linked-target")
            return result

        backend.snapshot = snapshot
        backend._raw_snapshot = snapshot
        backend._raw_snapshot = raw_snapshot

        with (
            patch.object(
                project_fs_module.secrets,
                "token_hex",
                return_value="a" * 24,
            ),
            patch.multiple(
                project_fs_module,
                _REPLACEFILE_FLAGS_NONE=0,
                create=True,
            ),
        ):
            with self.assertRaisesRegex(
                Exception,
                r"target\.txt: path-identity-changed",
            ):
                backend._replace_with_backup_checked(
                    root / source,
                    source,
                    source_snapshot.identity,
                    root / destination,
                    destination,
                    destination_snapshot,
                )

        self.assertEqual(backend.api.calls, 2)
        self.assertEqual(parked, [destination_snapshot])
        self.assertEqual(state[source], source_snapshot)
        self.assertEqual(state[destination], attacker_snapshot)
        self.assertEqual(state[backup], missing)

    def test_windows_replacefile_restores_raced_source_entry(self) -> None:
        from core import project_fs as project_fs_module
        from core.project_fs import _PathIdentity, _PathSnapshot, _WindowsBackend

        root = Path("C:/project")
        source = Path("source.txt")
        destination = Path("target.txt")
        backup = Path(
            ".target.txt.kafa-displaced-aaaaaaaaaaaaaaaaaaaaaaaa.tmp"
        )
        missing = _PathSnapshot(False, None)

        def present(file_id: bytes) -> _PathSnapshot:
            return _PathSnapshot(
                True,
                _PathIdentity(1, file_id, "file", 0, 1),
            )

        source_snapshot = present(b"verified-source")
        destination_snapshot = present(b"destination")
        attacker_snapshot = present(b"raced-source")
        state = {
            source: source_snapshot,
            destination: destination_snapshot,
            backup: missing,
        }
        parked: list[_PathSnapshot] = []

        class FakeApi:
            calls = 0

            def ReplaceFileW(self, *_args) -> bool:
                self.calls += 1
                if self.calls == 1:
                    parked.append(state[source])
                    state[destination] = attacker_snapshot
                    state[source] = missing
                    state[backup] = destination_snapshot
                else:
                    state[source] = state[destination]
                    state[destination] = state[backup]
                    state[backup] = missing
                return True

            @staticmethod
            def error() -> OSError:
                return OSError(5, "unexpected fake API failure")

        backend = object.__new__(_WindowsBackend)
        backend.root = root
        backend.api = FakeApi()

        def snapshot(relative: Path, *, allow_missing: bool, **_kwargs):
            result = state.get(Path(relative), missing)
            if not allow_missing and not result.exists:
                raise AssertionError(f"unexpected missing path: {relative}")
            return result

        backend.snapshot = snapshot
        backend._raw_snapshot = snapshot

        with (
            patch.object(
                project_fs_module.secrets,
                "token_hex",
                return_value="a" * 24,
            ),
            patch.multiple(
                project_fs_module,
                _REPLACEFILE_FLAGS_NONE=0,
                create=True,
            ),
        ):
            with self.assertRaisesRegex(Exception, "path-identity-changed"):
                backend._replace_with_backup_checked(
                    root / source,
                    source,
                    source_snapshot.identity,
                    root / destination,
                    destination,
                    destination_snapshot,
                )

        self.assertEqual(backend.api.calls, 2)
        self.assertEqual(parked, [source_snapshot])
        self.assertEqual(state[source], attacker_snapshot)
        self.assertEqual(state[destination], destination_snapshot)
        self.assertEqual(state[backup], missing)

    def test_windows_replacefile_cancellation_restores_original_state(self) -> None:
        from core import project_fs as project_fs_module
        from core.project_fs import _PathIdentity, _PathSnapshot, _WindowsBackend

        root = Path("C:/project")
        source = Path("source.txt")
        destination = Path("target.txt")
        backup = Path(
            ".target.txt.kafa-displaced-aaaaaaaaaaaaaaaaaaaaaaaa.tmp"
        )
        missing = _PathSnapshot(False, None)
        source_identity = _PathIdentity(1, b"source", "file", 0, 1)
        destination_identity = _PathIdentity(1, b"destination", "file", 0, 1)
        source_snapshot = _PathSnapshot(True, source_identity)
        destination_snapshot = _PathSnapshot(True, destination_identity)
        state = {
            source: source_snapshot,
            destination: destination_snapshot,
            backup: missing,
        }

        class FakeApi:
            calls = 0

            def ReplaceFileW(self, *_args) -> bool:
                self.calls += 1
                if self.calls == 1:
                    state[destination] = source_snapshot
                    state[source] = missing
                    state[backup] = destination_snapshot
                    raise KeyboardInterrupt(
                        "injected ReplaceFileW cancellation"
                    )
                state[source] = state[destination]
                state[destination] = state[backup]
                state[backup] = missing
                return True

            @staticmethod
            def error() -> OSError:
                return OSError(5, "unexpected fake API failure")

        backend = object.__new__(_WindowsBackend)
        backend.root = root
        backend.api = FakeApi()

        def snapshot(relative: Path, *, allow_missing: bool, **_kwargs):
            result = state.get(Path(relative), missing)
            if not allow_missing and not result.exists:
                raise AssertionError(f"unexpected missing path: {relative}")
            return result

        backend.snapshot = snapshot
        backend._raw_snapshot = snapshot

        with (
            patch.object(
                project_fs_module.secrets,
                "token_hex",
                return_value="a" * 24,
            ),
            patch.multiple(
                project_fs_module,
                _REPLACEFILE_FLAGS_NONE=0,
                create=True,
            ),
        ):
            with self.assertRaisesRegex(
                KeyboardInterrupt,
                "injected ReplaceFileW cancellation",
            ):
                backend._replace_with_backup_checked(
                    root / source,
                    source,
                    source_identity,
                    root / destination,
                    destination,
                    destination_snapshot,
                )

        self.assertEqual(backend.api.calls, 2)
        self.assertEqual(state[source], source_snapshot)
        self.assertEqual(state[destination], destination_snapshot)
        self.assertEqual(state[backup], missing)

    def test_windows_replace_finalization_cancellation_rolls_back(self) -> None:
        from core import project_fs as project_fs_module
        from core.project_fs import (
            ProjectPathSafetyError,
            _PathIdentity,
            _PathSnapshot,
            _WindowsBackend,
        )

        root = Path("C:/project")
        source = Path("source.txt")
        destination = Path("target.txt")
        backup = Path(
            ".target.txt.kafa-displaced-aaaaaaaaaaaaaaaaaaaaaaaa.tmp"
        )
        missing = _PathSnapshot(False, None)
        source_identity = _PathIdentity(1, b"source", "file", 0, 1)
        destination_identity = _PathIdentity(1, b"destination", "file", 0, 1)
        source_snapshot = _PathSnapshot(True, source_identity)
        destination_snapshot = _PathSnapshot(True, destination_identity)

        for stage in ("open", "attributes", "flush", "backup-delete"):
            with self.subTest(stage=stage):
                state = {
                    source: source_snapshot,
                    destination: destination_snapshot,
                    backup: missing,
                }

                class FakeApi:
                    calls = 0

                    def ReplaceFileW(self, *_args) -> bool:
                        self.calls += 1
                        if self.calls == 1:
                            state[destination] = source_snapshot
                            state[source] = missing
                            state[backup] = destination_snapshot
                        else:
                            state[source] = state[destination]
                            state[destination] = state[backup]
                            state[backup] = missing
                        return True

                    @staticmethod
                    def error() -> OSError:
                        return OSError(5, "unexpected fake API failure")

                    @staticmethod
                    def FlushFileBuffers(_handle) -> bool:
                        if stage == "flush":
                            raise KeyboardInterrupt(
                                "injected finalization cancellation"
                            )
                        return True

                backend = object.__new__(_WindowsBackend)
                backend.root = root
                backend.api = FakeApi()

                def snapshot(relative: Path, *, allow_missing: bool, **_kwargs):
                    result = state.get(Path(relative), missing)
                    if not allow_missing and not result.exists:
                        raise AssertionError(f"unexpected missing path: {relative}")
                    return result

                def apply_attributes(*_args, **_kwargs) -> None:
                    if stage == "attributes":
                        raise KeyboardInterrupt(
                            "injected finalization cancellation"
                        )

                def unlink_regular(*_args, **_kwargs) -> None:
                    if stage == "backup-delete":
                        raise KeyboardInterrupt(
                            "injected finalization cancellation"
                        )
                    state[backup] = missing

                def open_handle(*_args, **_kwargs):
                    if stage == "open":
                        raise OSError(32, "injected sharing violation")
                    return 101

                backend.snapshot = snapshot
                backend._raw_snapshot = snapshot
                backend._open_handle = open_handle
                backend._identity = lambda *_args, **_kwargs: source_identity
                backend._close = lambda _handle: None
                backend._apply_file_attributes = apply_attributes
                backend.unlink_regular = unlink_regular

                with (
                    patch.object(
                        project_fs_module.secrets,
                        "token_hex",
                        return_value="a" * 24,
                    ),
                    patch.multiple(
                        project_fs_module,
                        _INVALID_HANDLE_VALUE=-1,
                        _GENERIC_READ=0x80000000,
                        _GENERIC_WRITE=0x40000000,
                        _FILE_WRITE_ATTRIBUTES=0x0100,
                        _REPLACEFILE_FLAGS_NONE=0,
                        create=True,
                    ),
                ):
                    if stage == "open":
                        with self.assertRaisesRegex(
                            ProjectPathSafetyError,
                            "path-identity-changed",
                        ):
                            backend._replace_with_backup_checked(
                                root / source,
                                source,
                                source_identity,
                                root / destination,
                                destination,
                                destination_snapshot,
                            )
                    else:
                        with self.assertRaisesRegex(
                            KeyboardInterrupt,
                            "injected finalization cancellation",
                        ):
                            backend._replace_with_backup_checked(
                                root / source,
                                source,
                                source_identity,
                                root / destination,
                                destination,
                                destination_snapshot,
                            )

                self.assertEqual(backend.api.calls, 2)
                self.assertEqual(state[source], source_snapshot)
                self.assertEqual(state[destination], destination_snapshot)
                self.assertEqual(state[backup], missing)

    def test_windows_handle_rename_cancellation_restores_missing_source(self) -> None:
        from core.project_fs import _PathIdentity, _PathSnapshot, _WindowsBackend

        source = Path("source.txt")
        destination = Path("target.txt")
        source_identity = _PathIdentity(1, b"source", "file", 0, 1)
        source_snapshot = _PathSnapshot(True, source_identity)
        missing = _PathSnapshot(False, None)
        state = {
            source: missing,
            destination: source_snapshot,
        }
        backend = object.__new__(_WindowsBackend)

        def snapshot(relative: Path, *, allow_missing: bool, **_kwargs):
            result = state[Path(relative)]
            if not allow_missing and not result.exists:
                raise AssertionError(f"unexpected missing path: {relative}")
            return result

        def rename_back(
            _handle,
            _destination_path,
            rollback_relative,
            **_kwargs,
        ) -> None:
            self.assertEqual(Path(rollback_relative), source)
            state[source] = state[destination]
            state[destination] = missing

        backend.snapshot = snapshot
        backend._rename_by_handle = rename_back
        cancellation = KeyboardInterrupt(
            "injected handle rename cancellation"
        )

        backend._reconcile_windows_handle_rename_interruption(
            101,
            source_identity,
            Path("C:/project/target.txt"),
            destination,
            Path("C:/project/source.txt"),
            source,
            cancellation,
        )

        self.assertEqual(state[source], source_snapshot)
        self.assertEqual(state[destination], missing)
        self.assertEqual(getattr(cancellation, "__notes__", ()), ())

    def test_windows_missing_destination_post_rename_failures_restore_source(self) -> None:
        from core.project_fs import _PathIdentity, _PathSnapshot, _WindowsBackend

        source = Path("source.txt")
        destination = Path("target.txt")
        source_path = Path("C:/project/source.txt")
        destination_path = Path("C:/project/target.txt")
        source_identity = _PathIdentity(1, b"source", "file", 0, 1)
        source_snapshot = _PathSnapshot(True, source_identity)
        missing = _PathSnapshot(False, None)

        for operation, stage, flush in (
            ("atomic-write", "identity", True),
            ("atomic-write", "flush", True),
            ("replace-file", "identity", False),
        ):
            with self.subTest(operation=operation, stage=stage):
                state = {
                    source: source_snapshot,
                    destination: missing,
                }
                backend = object.__new__(_WindowsBackend)
                rename_calls = 0

                def snapshot(relative: Path, *, allow_missing: bool, **_kwargs):
                    result = state[Path(relative)]
                    if not allow_missing and not result.exists:
                        raise AssertionError(f"unexpected missing path: {relative}")
                    return result

                def rename(
                    _handle,
                    _destination_path,
                    relative,
                    **_kwargs,
                ) -> None:
                    nonlocal rename_calls
                    rename_calls += 1
                    if rename_calls == 1:
                        state[destination] = state[source]
                        state[source] = missing
                    else:
                        self.assertEqual(Path(relative), source)
                        state[source] = state[destination]
                        state[destination] = missing

                def identity(*_args, **_kwargs):
                    if stage == "identity" and rename_calls == 1:
                        raise KeyboardInterrupt("injected post-rename identity cancellation")
                    return source_identity

                class FakeApi:
                    @staticmethod
                    def FlushFileBuffers(_handle) -> bool:
                        if stage == "flush":
                            raise KeyboardInterrupt("injected post-rename flush cancellation")
                        return True

                backend.snapshot = snapshot
                backend._rename_by_handle = rename
                backend._identity = identity
                backend.api = FakeApi()

                with self.assertRaisesRegex(KeyboardInterrupt, "post-rename"):
                    backend._publish_handle_rename_checked(
                        101,
                        source_path,
                        source,
                        source_identity,
                        destination_path,
                        destination,
                        flush=flush,
                    )

                self.assertEqual(rename_calls, 2)
                self.assertEqual(state[source], source_snapshot)
                self.assertEqual(state[destination], missing)

    def test_windows_reparse_ancestor_maps_to_unsafe_ancestor(self) -> None:
        from core.project_fs import (
            ProjectPathSafetyError,
            _PathIdentity,
            _WindowsBackend,
        )

        with tempfile.TemporaryDirectory() as temp:
            backend = object.__new__(_WindowsBackend)
            backend.root = Path(temp)
            backend.root_identity = _PathIdentity(
                volume=1,
                file_id=b"root",
                kind="directory",
                mode_or_attributes=0,
                nlink=0,
            )
            handles = iter((101, 102))
            backend._open_handle = lambda *_args, **_kwargs: next(handles)
            backend._close = lambda _handle: None

            def identity(handle, **_kwargs):
                if handle == 101:
                    return backend.root_identity
                raise ProjectPathSafetyError(
                    Path("junction/result.txt"),
                    "unsafe-target",
                )

            backend._identity = identity
            with self.assertRaisesRegex(
                Exception,
                "unsafe-project-path: junction/result.txt: unsafe-ancestor",
            ):
                with backend._ancestors(
                    Path("junction/result.txt"),
                    create=False,
                ):
                    self.fail("unsafe Windows ancestor was accepted")

    def test_windows_snapshot_allows_a_missing_ancestor_when_missing_is_allowed(self) -> None:
        from core.project_fs import (
            _PathIdentity,
            _PathSnapshot,
            _WindowsBackend,
        )

        with tempfile.TemporaryDirectory() as temp:
            backend = object.__new__(_WindowsBackend)
            backend.root = Path(temp)
            backend.root_identity = _PathIdentity(
                volume=1,
                file_id=b"root",
                kind="directory",
                mode_or_attributes=0,
                nlink=0,
            )

            def open_handle(path: Path, **_kwargs):
                if Path(path) == backend.root:
                    return 101
                raise FileNotFoundError(2, "injected missing ancestor")

            backend._open_handle = open_handle
            backend._close = lambda _handle: None
            backend._identity = lambda *_args, **_kwargs: backend.root_identity

            relative = Path(".ai-team/state/local-core-migration.lock")
            for operation in (backend.snapshot, backend._raw_snapshot):
                with self.subTest(operation=operation.__name__):
                    self.assertEqual(
                        operation(relative, allow_missing=True),
                        _PathSnapshot(False),
                    )

    def test_windows_known_snapshot_recheck_normalizes_raced_path_classes(self) -> None:
        from core.project_fs import (
            ProjectPathSafetyError,
            _PathIdentity,
            _PathSnapshot,
            _WindowsBackend,
        )

        backend = object.__new__(_WindowsBackend)
        relative = Path("state/result.txt")
        expected = _PathSnapshot(
            True,
            _PathIdentity(1, b"expected", "file", 0, 1),
        )
        for reason in (
            "unsafe-ancestor",
            "unsafe-target",
            "hard-linked-target",
            "cross-device-ancestor",
        ):
            with self.subTest(reason=reason):
                backend.snapshot = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    ProjectPathSafetyError(relative, reason)
                )
                with self.assertRaisesRegex(
                    ProjectPathSafetyError,
                    "path-identity-changed",
                ):
                    backend._recheck_snapshot(relative, expected)

        backend.snapshot = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProjectPathSafetyError(relative, "platform-safety-unavailable")
        )
        with self.assertRaisesRegex(
            ProjectPathSafetyError,
            "platform-safety-unavailable",
        ):
            backend._recheck_snapshot(relative, expected)

    def test_windows_zero_link_identity_is_changed_not_hard_link(self) -> None:
        from core.project_fs import (
            ProjectPathSafetyError,
            _PathIdentity,
            _WindowsBackend,
        )

        backend = object.__new__(_WindowsBackend)
        for links, expected_reason in (
            (0, "path-identity-changed"),
            (2, "hard-linked-target"),
        ):
            with self.subTest(links=links):
                backend._raw_identity = lambda *_args, **_kwargs: _PathIdentity(
                    1,
                    b"leaf",
                    "file",
                    0,
                    links,
                )
                with self.assertRaisesRegex(
                    ProjectPathSafetyError,
                    expected_reason,
                ):
                    backend._identity(
                        101,
                        expect_directory=False,
                        relative=Path("leaf.txt"),
                    )

    def test_windows_directory_creation_uses_only_pinned_parent_handle(self) -> None:
        from core.project_fs import _PathIdentity, _WindowsBackend

        calls: list[tuple[int, str]] = []

        class FakeApi:
            @staticmethod
            def create_directory_relative(parent_handle: int, leaf: str) -> int:
                calls.append((parent_handle, leaf))
                return 303

        backend = object.__new__(_WindowsBackend)
        backend.api = FakeApi()
        identity = _PathIdentity(1, b"directory", "directory", 0, 0)
        backend.root_identity = _PathIdentity(1, b"root", "directory", 0, 0)
        backend._identity = lambda *_args, **_kwargs: identity

        handle, actual = backend._create_directory_at(
            202,
            "schema-0123456789abcdef",
            Path("backups/schema-0123456789abcdef"),
        )

        self.assertEqual(calls, [(202, "schema-0123456789abcdef")])
        self.assertEqual(handle, 303)
        self.assertEqual(actual, identity)

    def test_windows_native_directory_create_uses_exact_relative_abi(self) -> None:
        import ctypes

        from core import project_fs as project_fs_module
        from core.project_fs import (
            _NT_FILE_CREATED,
            _NT_FILE_CREATE,
            _NT_FILE_DIRECTORY_FILE,
            _NT_FILE_READ_ATTRIBUTES,
            _NT_FILE_SYNCHRONOUS_IO_NONALERT,
            _NT_FILE_WRITE_THROUGH,
            _NT_IO_STATUS_BLOCK,
            _NT_OBJECT_ATTRIBUTES,
            _NT_OBJ_CASE_INSENSITIVE,
            _NT_SYNCHRONIZE,
            _NT_UNICODE_STRING,
            _WindowsApi,
        )

        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(
            ctypes.sizeof(_NT_UNICODE_STRING),
            16 if pointer_size == 8 else 8,
        )
        self.assertEqual(
            ctypes.sizeof(_NT_OBJECT_ATTRIBUTES),
            48 if pointer_size == 8 else 24,
        )
        self.assertEqual(
            ctypes.sizeof(_NT_IO_STATUS_BLOCK),
            16 if pointer_size == 8 else 8,
        )

        captured: dict[str, object] = {}
        closed: list[int] = []

        def nt_create_file(
            handle_pointer,
            desired_access,
            object_attributes_pointer,
            status_block_pointer,
            allocation_size,
            file_attributes,
            share_access,
            create_disposition,
            create_options,
            ea_buffer,
            ea_length,
        ) -> int:
            attributes = ctypes.cast(
                object_attributes_pointer,
                ctypes.POINTER(_NT_OBJECT_ATTRIBUTES),
            ).contents
            name = attributes.object_name.contents
            encoded = ctypes.string_at(name.buffer, name.length)
            terminated = ctypes.string_at(name.buffer, name.maximum_length)
            captured.update(
                desired_access=int(desired_access),
                root_directory=int(attributes.root_directory),
                object_attributes=int(attributes.attributes),
                leaf=encoded.decode("utf-16-le"),
                length=int(name.length),
                maximum_length=int(name.maximum_length),
                terminated=terminated,
                allocation_size=allocation_size,
                file_attributes=int(file_attributes),
                share_access=int(share_access),
                create_disposition=int(create_disposition),
                create_options=int(create_options),
                ea_buffer=ea_buffer,
                ea_length=int(ea_length),
            )
            ctypes.cast(
                handle_pointer,
                ctypes.POINTER(ctypes.c_void_p),
            ).contents.value = 303
            ctypes.cast(
                status_block_pointer,
                ctypes.POINTER(_NT_IO_STATUS_BLOCK),
            ).contents.information = _NT_FILE_CREATED
            return 0

        api = object.__new__(_WindowsApi)
        api.NtCreateFile = nt_create_file
        api.RtlNtStatusToDosError = lambda _status: self.fail(
            "successful NtCreateFile must not translate status"
        )
        api.CloseHandle = lambda handle: closed.append(int(handle))
        leaf = "schema-rocket-\U0001F680"
        encoded = leaf.encode("utf-16-le")

        with patch.multiple(
            project_fs_module,
            _DELETE=0x00010000,
            _FILE_ATTRIBUTE_NORMAL=0x00000080,
            _FILE_SHARE_READ=0x00000001,
            _FILE_SHARE_WRITE=0x00000002,
            _INVALID_HANDLE_VALUE=ctypes.c_void_p(-1).value,
            create=True,
        ):
            handle = api.create_directory_relative(202, leaf)

        self.assertEqual(handle, 303)
        self.assertEqual(closed, [])
        self.assertEqual(captured["root_directory"], 202)
        self.assertEqual(captured["leaf"], leaf)
        self.assertEqual(captured["length"], len(encoded))
        self.assertEqual(captured["maximum_length"], len(encoded) + 2)
        self.assertTrue(bytes(captured["terminated"]).endswith(b"\x00\x00"))
        self.assertEqual(captured["object_attributes"], _NT_OBJ_CASE_INSENSITIVE)
        self.assertEqual(
            captured["desired_access"],
            0x00010000 | _NT_FILE_READ_ATTRIBUTES | _NT_SYNCHRONIZE,
        )
        self.assertEqual(captured["share_access"], 0x00000003)
        self.assertEqual(captured["create_disposition"], _NT_FILE_CREATE)
        self.assertEqual(
            captured["create_options"],
            _NT_FILE_DIRECTORY_FILE
            | _NT_FILE_WRITE_THROUGH
            | _NT_FILE_SYNCHRONOUS_IO_NONALERT,
        )
        self.assertEqual(captured["allocation_size"], None)
        self.assertEqual(captured["ea_buffer"], None)
        self.assertEqual(captured["ea_length"], 0)

    def test_windows_native_directory_create_fails_closed_on_status_anomalies(
        self,
    ) -> None:
        import ctypes

        from core import project_fs as project_fs_module
        from core.project_fs import (
            _NT_ERROR_MR_MID_NOT_FOUND,
            _NT_FILE_CREATED,
            _NT_IO_STATUS_BLOCK,
            _NT_STATUS_OBJECT_NAME_COLLISION,
            _WindowsApi,
            _WindowsCapabilityError,
        )

        closed: list[int] = []
        api = object.__new__(_WindowsApi)
        api.CloseHandle = lambda handle: closed.append(int(handle))
        constants = {
            "_DELETE": 0x00010000,
            "_FILE_ATTRIBUTE_NORMAL": 0x00000080,
            "_FILE_SHARE_READ": 0x00000001,
            "_FILE_SHARE_WRITE": 0x00000002,
            "_INVALID_HANDLE_VALUE": ctypes.c_void_p(-1).value,
        }

        with patch.multiple(project_fs_module, **constants, create=True):
            api.NtCreateFile = lambda *_args: ctypes.c_int32(
                _NT_STATUS_OBJECT_NAME_COLLISION
            ).value
            api.RtlNtStatusToDosError = lambda _status: self.fail(
                "collision must not use the generic status converter"
            )
            with self.assertRaises(FileExistsError):
                api.create_directory_relative(202, "collision")

            def unmapped_status(handle_pointer, *_args) -> int:
                ctypes.cast(
                    handle_pointer,
                    ctypes.POINTER(ctypes.c_void_p),
                ).contents.value = 405
                return -1

            api.NtCreateFile = unmapped_status
            api.RtlNtStatusToDosError = (
                lambda _status: _NT_ERROR_MR_MID_NOT_FOUND
            )
            with self.assertRaises(_WindowsCapabilityError):
                api.create_directory_relative(202, "unmapped")

            def unexpected_success(handle_pointer, *_args) -> int:
                ctypes.cast(
                    handle_pointer,
                    ctypes.POINTER(ctypes.c_void_p),
                ).contents.value = 404
                status_block_pointer = _args[2]
                ctypes.cast(
                    status_block_pointer,
                    ctypes.POINTER(_NT_IO_STATUS_BLOCK),
                ).contents.information = _NT_FILE_CREATED - 1
                return 0

            api.NtCreateFile = unexpected_success
            api.RtlNtStatusToDosError = lambda _status: self.fail(
                "successful status must not use the converter"
            )
            with self.assertRaises(_WindowsCapabilityError):
                api.create_directory_relative(202, "unexpected")

            def interrupted(handle_pointer, *_args) -> int:
                ctypes.cast(
                    handle_pointer,
                    ctypes.POINTER(ctypes.c_void_p),
                ).contents.value = 505
                raise KeyboardInterrupt("injected native create cancellation")

            api.NtCreateFile = interrupted
            with self.assertRaisesRegex(
                KeyboardInterrupt,
                "injected native create cancellation",
            ):
                api.create_directory_relative(202, "interrupted")

        self.assertEqual(closed, [405, 404, 505])

    def test_windows_readonly_replacement_uses_exact_writable_handle_and_restores(self) -> None:
        from core import project_fs as project_fs_module
        from core.project_fs import _PathIdentity, _PathSnapshot, _WindowsBackend

        readonly = 0x01
        normal = 0x80
        attributes = readonly
        applied: list[int] = []
        backend = object.__new__(_WindowsBackend)
        relative = Path("state/readonly.txt")

        def apply_attributes(_handle, _relative, value) -> None:
            nonlocal attributes
            applied.append(int(value))
            attributes = int(value) or normal

        def identity(*_args, **_kwargs):
            return _PathIdentity(1, b"readonly", "file", attributes, 1)

        backend._apply_file_attributes = apply_attributes
        backend._identity = identity
        backend._raw_identity = identity
        backend.snapshot = lambda *_args, **_kwargs: _PathSnapshot(
            True,
            identity(),
        )
        original = _PathSnapshot(True, identity())

        with patch.multiple(
            project_fs_module,
            _FILE_ATTRIBUTE_READONLY=readonly,
            _FILE_ATTRIBUTE_NORMAL=normal,
            create=True,
        ):
            with self.assertRaisesRegex(KeyboardInterrupt, "cancel replacement"):
                with backend._temporarily_writable_destination(
                    101,
                    relative,
                    original,
                ) as lease:
                    self.assertFalse(attributes & readonly)
                    self.assertEqual(
                        lease.working_snapshot.identity.mode_or_attributes,
                        normal,
                    )
                    raise KeyboardInterrupt("cancel replacement")

        self.assertEqual(applied, [0, readonly])
        self.assertEqual(attributes, readonly)

    def test_windows_readonly_replacement_source_is_prepared_through_pinned_handle(
        self,
    ) -> None:
        from core import project_fs as project_fs_module
        from core.project_fs import _PathIdentity, _PathSnapshot, _WindowsBackend

        readonly = 0x01
        normal = 0x80
        attributes = readonly
        applied: list[int] = []
        recheck_share_delete: list[bool] = []
        backend = object.__new__(_WindowsBackend)
        relative = Path("state/replacement.txt")
        original = _PathIdentity(1, b"replacement", "file", readonly, 1)

        def identity(*_args, **_kwargs):
            return _PathIdentity(1, b"replacement", "file", attributes, 1)

        def apply_attributes(_handle, _relative, value) -> None:
            nonlocal attributes
            applied.append(int(value))
            attributes = int(value) or normal

        backend._identity = identity
        backend._raw_identity = identity
        backend._apply_file_attributes = apply_attributes

        def recheck(*_args, **kwargs):
            recheck_share_delete.append(
                bool(kwargs.get("leaf_share_delete", False))
            )
            return _PathSnapshot(True, identity())

        backend._recheck_snapshot = recheck

        with patch.multiple(
            project_fs_module,
            _FILE_ATTRIBUTE_READONLY=readonly,
            _FILE_ATTRIBUTE_NORMAL=normal,
            create=True,
        ):
            backend._make_replacement_source_writable(
                101,
                relative,
                original,
            )

        self.assertEqual(applied, [0])
        self.assertEqual(attributes, normal)
        self.assertEqual(recheck_share_delete, [True])

    def test_windows_snapshot_share_delete_is_scoped_to_the_leaf_handle(
        self,
    ) -> None:
        from core.project_fs import _PathIdentity, _WindowsBackend

        backend = object.__new__(_WindowsBackend)
        backend.root_identity = _PathIdentity(
            1,
            b"root",
            "directory",
            0x10,
            0,
        )
        leaf_identity = _PathIdentity(1, b"leaf", "file", 0x80, 1)
        opened: list[dict[str, object]] = []

        @contextmanager
        def ancestors(*_args, **_kwargs):
            yield Path("C:/project/state")

        def open_handle(_path, **kwargs):
            opened.append(dict(kwargs))
            return 101

        backend._ancestors = ancestors
        backend._open_handle = open_handle
        backend._identity = lambda *_args, **_kwargs: leaf_identity
        backend._close = lambda _handle: None

        shared = backend.snapshot(
            Path("state/replacement.txt"),
            allow_missing=False,
            leaf_share_delete=True,
        )
        ordinary = backend.snapshot(
            Path("state/replacement.txt"),
            allow_missing=False,
        )

        self.assertEqual(shared.identity, leaf_identity)
        self.assertEqual(ordinary.identity, leaf_identity)
        self.assertEqual(
            [entry.get("share_delete") for entry in opened],
            [True, False],
        )

    def test_windows_replacement_source_preflight_restores_on_path_exchange(
        self,
    ) -> None:
        from core import project_fs as project_fs_module
        from core.project_fs import _PathIdentity, _PathSnapshot, _WindowsBackend

        readonly = 0x01
        normal = 0x80
        attributes = readonly
        applied: list[int] = []
        recheck_share_delete: list[bool] = []
        backend = object.__new__(_WindowsBackend)
        relative = Path("state/replacement.txt")
        original = _PathIdentity(1, b"replacement", "file", readonly, 1)
        attacker = _PathIdentity(1, b"attacker", "file", normal, 1)

        def identity(*_args, **_kwargs):
            return _PathIdentity(1, b"replacement", "file", attributes, 1)

        def apply_attributes(_handle, _relative, value) -> None:
            nonlocal attributes
            applied.append(int(value))
            attributes = int(value) or normal

        backend._identity = identity
        backend._raw_identity = identity
        backend._apply_file_attributes = apply_attributes

        def recheck(*_args, **kwargs):
            recheck_share_delete.append(
                bool(kwargs.get("leaf_share_delete", False))
            )
            return _PathSnapshot(True, attacker)

        backend._recheck_snapshot = recheck

        with patch.multiple(
            project_fs_module,
            _FILE_ATTRIBUTE_READONLY=readonly,
            _FILE_ATTRIBUTE_NORMAL=normal,
            create=True,
        ):
            with self.assertRaisesRegex(
                Exception,
                "path-identity-changed",
            ):
                backend._make_replacement_source_writable(
                    101,
                    relative,
                    original,
                )

        self.assertEqual(applied, [0, readonly])
        self.assertEqual(attributes, readonly)
        self.assertEqual(recheck_share_delete, [True])

    def test_windows_readonly_destination_pin_allows_exact_attribute_restore(self) -> None:
        from core import project_fs as project_fs_module
        from core.project_fs import _PathIdentity, _PathSnapshot, _WindowsBackend

        readonly = 0x01
        write_attributes = 0x0100
        expected = _PathSnapshot(
            True,
            _PathIdentity(1, b"readonly", "file", readonly, 1),
        )
        opened: list[dict[str, object]] = []
        backend = object.__new__(_WindowsBackend)

        def open_handle(_path, **kwargs):
            opened.append(kwargs)
            return 101

        backend._open_handle = open_handle
        backend._identity = lambda *_args, **_kwargs: expected.identity

        with patch.multiple(
            project_fs_module,
            _FILE_ATTRIBUTE_READONLY=readonly,
            _FILE_WRITE_ATTRIBUTES=write_attributes,
            create=True,
        ):
            handle, identity = backend._pin_destination(
                Path("C:/project/state/readonly.txt"),
                Path("state/readonly.txt"),
                expected,
            )

        self.assertEqual(handle, 101)
        self.assertEqual(identity, expected.identity)
        self.assertEqual(len(opened), 1)
        self.assertTrue(int(opened[0]["access"]) & write_attributes)
        self.assertIs(opened[0]["share_delete"], True)

    def test_windows_disposed_readonly_destination_preserves_primary_error(
        self,
    ) -> None:
        from core import project_fs as project_fs_module
        from core.project_fs import _PathIdentity, _PathSnapshot, _WindowsBackend

        readonly = 0x01
        normal = 0x80
        attributes = readonly
        disposed = False
        applied: list[int] = []
        backend = object.__new__(_WindowsBackend)
        relative = Path("state/readonly.txt")

        def identity(*_args, **_kwargs):
            return _PathIdentity(
                1,
                b"readonly",
                "file",
                attributes,
                0 if disposed else 1,
            )

        def apply_attributes(_handle, _relative, value) -> None:
            nonlocal attributes
            applied.append(int(value))
            attributes = int(value) or normal

        backend._identity = identity
        backend._raw_identity = identity
        backend._apply_file_attributes = apply_attributes
        backend.snapshot = lambda *_args, **_kwargs: _PathSnapshot(
            True,
            identity(),
        )
        original = _PathSnapshot(
            True,
            _PathIdentity(1, b"readonly", "file", readonly, 1),
        )

        with patch.multiple(
            project_fs_module,
            _FILE_ATTRIBUTE_READONLY=readonly,
            _FILE_ATTRIBUTE_NORMAL=normal,
            create=True,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "post-delete verification failed",
            ) as raised:
                with backend._temporarily_writable_destination(
                    101,
                    relative,
                    original,
                ) as lease:
                    disposed = True
                    raise RuntimeError("post-delete verification failed")

        self.assertTrue(lease.discarded)
        self.assertEqual(applied, [0])
        self.assertEqual(getattr(raised.exception, "__notes__", ()), ())

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

    def test_windows_handle_rename_buffer_contains_a_terminated_utf16_path(self) -> None:
        import ctypes

        from core import project_fs as project_fs_module
        from core.project_fs import _WindowsBackend

        class FakeRenameInfo(ctypes.Structure):
            _fields_ = (
                ("flags", ctypes.c_uint32),
                ("root_directory", ctypes.c_void_p),
                ("file_name_length", ctypes.c_uint32),
                ("file_name", ctypes.c_uint16 * 1),
            )

        captured: dict[str, object] = {}

        class FakeApi:
            @staticmethod
            def SetFileInformationByHandle(
                _handle,
                information_class,
                buffer,
                buffer_size,
            ) -> bool:
                information = ctypes.cast(
                    buffer,
                    ctypes.POINTER(FakeRenameInfo),
                ).contents
                captured.update(
                    information_class=information_class,
                    file_name_length=int(information.file_name_length),
                    buffer_size=buffer_size,
                    raw=ctypes.string_at(buffer, buffer_size),
                )
                return True

        backend = object.__new__(_WindowsBackend)
        backend.api = FakeApi()
        destination = Path("C:/project/state/target.txt")
        encoded = os.fspath(destination).encode("utf-16-le")
        offset = FakeRenameInfo.file_name.offset

        with patch.multiple(
            project_fs_module,
            _FILE_RENAME_INFO=FakeRenameInfo,
            _FILE_RENAME_INFO_EX_CLASS=22,
            _FILE_RENAME_REPLACE_IF_EXISTS=0x01,
            _FILE_RENAME_POSIX_SEMANTICS=0x02,
            _FILE_RENAME_IGNORE_READONLY_ATTRIBUTE=0x40,
            create=True,
        ):
            backend._rename_by_handle(
                101,
                destination,
                Path("state/target.txt"),
                replace_existing=False,
            )

        raw = captured["raw"]
        self.assertIsInstance(raw, bytes)
        assert isinstance(raw, bytes)
        self.assertEqual(captured["information_class"], 22)
        self.assertEqual(captured["file_name_length"], len(encoded))
        self.assertGreaterEqual(
            captured["buffer_size"],
            ctypes.sizeof(FakeRenameInfo) + len(encoded),
        )
        self.assertEqual(raw[offset : offset + len(encoded)], encoded)
        self.assertEqual(raw[offset + len(encoded) : offset + len(encoded) + 2], b"\0\0")

    @unittest.skipUnless(os.name == "nt", "Windows partial-write cleanup contract")
    def test_windows_partial_write_preserves_primary_error_and_cleans_target(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            unrelated = root / "unrelated.txt"
            unrelated.write_bytes(b"must-remain\n")
            with project_fs(root) as fs:
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
                with project_fs(root) as fs:
                    fs.atomic_write(Path("state.txt"), b"after\n")

            self.assertEqual(target.read_bytes(), b"before\n")
            self.assertEqual(tuple(root.glob(".state.txt.kafa-*.tmp")), ())

    @unittest.skipIf(os.name == "nt", "POSIX cleanup diagnostics contract")
    def test_atomic_write_preserves_primary_and_notes_cleanup_residual(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "state.txt"
            target.write_bytes(b"before\n")
            real_unlink = project_fs_module.os.unlink
            cleanup_failed = False

            def fail_write(_backend, descriptor: int, data: bytes) -> None:
                os.write(descriptor, data[:1])
                raise RuntimeError("primary write boom")

            def fail_cleanup(path, *args, **kwargs):
                nonlocal cleanup_failed
                if ".kafa-delete-" in os.fspath(path) and not cleanup_failed:
                    cleanup_failed = True
                    raise PermissionError("cleanup unlink boom")
                return real_unlink(path, *args, **kwargs)

            with (
                project_fs(root) as fs,
                patch.object(
                    project_fs_module._PosixBackend,
                    "_write_all",
                    new=fail_write,
                ),
                patch.object(
                    project_fs_module.os,
                    "unlink",
                    side_effect=fail_cleanup,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "primary write boom") as raised:
                    fs.atomic_write(Path("state.txt"), b"after\n")

            self.assertTrue(cleanup_failed)
            self.assertEqual(target.read_bytes(), b"before\n")
            residuals = tuple(root.glob(".state.txt.kafa-*.tmp"))
            self.assertEqual(len(residuals), 1)
            self.assertIn(
                os.fspath(residuals[0].name),
                "\n".join(getattr(raised.exception, "__notes__", ())),
            )
            self.assertIn(
                "cleanup unlink boom",
                "\n".join(getattr(raised.exception, "__notes__", ())),
            )


if __name__ == "__main__":
    unittest.main()
