"""No-publish release rehearsal for exact Kafa wheel/sdist candidates."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifact_subject import (
    ArtifactSubject,
    ArtifactSubjectError,
    assert_exact_subjects,
    subjects_by_kind,
)
from .cli import PLUGIN_NAME, managed_tree_is_safe, path_is_link, plugin_tree_digest
from .release import release_report
from .supply_chain import (
    SupplyChainError,
    _git,
    _ignored_source_path,
    generate_release_evidence,
    load_tooling,
    source_identity,
    verify_release_evidence,
)


REPORT_VERSION = "kafa-release-rehearsal-v1"


class RehearsalError(RuntimeError):
    """Raised when the no-publish rehearsal cannot prove its invariants."""


_REPORT_KEYS = {
    "ok",
    "report_version",
    "evidence_mode",
    "source",
    "build",
    "syft",
    "artifact_count",
    "sbom_count",
    "artifacts",
    "supply_chain_assurance",
    "isolated_install",
    "user_installation_before",
    "user_installation_after",
    "steps",
    "commands",
    "invariants",
    "external_effects",
    "generated_at",
}
_STEPS = [
    "snapshot",
    "build",
    "generate",
    "verify-before-install",
    "isolated-install",
    "verify-after-install",
]
_EXTERNAL_EFFECTS = {
    "tag": False,
    "release": False,
    "upload": False,
    "deployment": False,
    "user_installation_change": False,
}


def rehearsal_report_errors(report: Mapping[str, Any]) -> list[str]:
    """Return complete contract errors for a persisted no-publish rehearsal."""

    if not isinstance(report, Mapping) or set(report) != _REPORT_KEYS:
        actual = sorted(report) if isinstance(report, Mapping) else type(report).__name__
        return [f"rehearsal report keys mismatch: actual={actual} expected={sorted(_REPORT_KEYS)}"]
    errors: list[str] = []
    if report.get("ok") is not True:
        errors.append("rehearsal report is not successful")
    if report.get("report_version") != REPORT_VERSION:
        errors.append("rehearsal report version is unsupported")
    if report.get("evidence_mode") != "local-no-publish-rehearsal":
        errors.append("rehearsal evidence mode is invalid")

    source = report.get("source")
    if not isinstance(source, Mapping) or set(source) != {
        "git_commit",
        "git_status_sha256",
        "source_tree_sha256",
        "source_file_count",
        "dirty",
    }:
        errors.append("rehearsal source shape is invalid")
    else:
        if not _hex_digest(source.get("git_commit"), lengths=(40, 64)):
            errors.append("rehearsal source commit is invalid")
        if not _hex_digest(source.get("git_status_sha256")):
            errors.append("rehearsal source status digest is invalid")
        if not _hex_digest(source.get("source_tree_sha256")):
            errors.append("rehearsal source tree digest is invalid")
        count = source.get("source_file_count")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            errors.append("rehearsal source file count is invalid")
        if not isinstance(source.get("dirty"), bool):
            errors.append("rehearsal source dirty flag is invalid")
        elif source.get("dirty") is False and source.get("git_status_sha256") != hashlib.sha256(b"").hexdigest():
            errors.append("clean rehearsal source has a non-empty status digest")

    build = report.get("build")
    started: datetime | None = None
    finished: datetime | None = None
    if not isinstance(build, Mapping) or set(build) != {
        "frontend",
        "backend",
        "python",
        "source_date_epoch",
        "started_at",
        "finished_at",
    }:
        errors.append("rehearsal build shape is invalid")
    else:
        for field in ("frontend", "backend", "python"):
            if not isinstance(build.get(field), str) or not build.get(field):
                errors.append(f"rehearsal build {field} is invalid")
        epoch = build.get("source_date_epoch")
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch <= 0:
            errors.append("rehearsal source_date_epoch is invalid")
        started = _report_timestamp(build.get("started_at"))
        finished = _report_timestamp(build.get("finished_at"))
        if started is None or finished is None:
            errors.append("rehearsal build timestamps are invalid")
        elif started > finished:
            errors.append("rehearsal build timestamps are reversed")

    syft = report.get("syft")
    if not isinstance(syft, Mapping) or set(syft) != {"version", "commit", "format"}:
        errors.append("rehearsal Syft shape is invalid")
    else:
        if not isinstance(syft.get("version"), str) or not syft.get("version"):
            errors.append("rehearsal Syft version is invalid")
        if not _hex_digest(syft.get("commit"), lengths=(40, 64)):
            errors.append("rehearsal Syft commit is invalid")
        if syft.get("format") != "cyclonedx-json@1.6":
            errors.append("rehearsal Syft format is invalid")

    artifact_records = report.get("artifacts")
    artifacts: dict[str, ArtifactSubject] = {}
    if not isinstance(artifact_records, list) or len(artifact_records) != 2:
        errors.append("rehearsal requires exactly two artifact records")
    else:
        try:
            parsed = []
            for record in artifact_records:
                if not isinstance(record, Mapping) or set(record) != {
                    "name",
                    "kind",
                    "sha256",
                    "sbom",
                    "sbom_sha256",
                }:
                    raise RehearsalError("artifact record shape is invalid")
                subject = ArtifactSubject(
                    name=record["name"],
                    kind=record["kind"],
                    sha256=record["sha256"],
                )
                if record.get("sbom") != f"{subject.name}.cdx.json" or not _hex_digest(
                    record.get("sbom_sha256")
                ):
                    raise RehearsalError("artifact SBOM subject is invalid")
                parsed.append(subject)
            artifacts = subjects_by_kind(parsed)
            if set(artifacts) != {"wheel", "sdist"}:
                raise RehearsalError("artifact kinds are incomplete")
        except (ArtifactSubjectError, RehearsalError, TypeError) as exc:
            errors.append(f"rehearsal artifact contract is invalid: {exc}")
    if report.get("artifact_count") != 2 or report.get("sbom_count") != 2:
        errors.append("rehearsal artifact or SBOM count is invalid")
    if report.get("supply_chain_assurance") != "unsigned-local-integrity-statement":
        errors.append("rehearsal supply-chain assurance is invalid")

    smoke = report.get("isolated_install")
    if not isinstance(smoke, dict):
        errors.append("rehearsal isolated install detail is invalid")
    elif artifacts:
        try:
            _validate_smoke(smoke, artifacts)
        except RehearsalError as exc:
            errors.append(str(exc))

    before = report.get("user_installation_before")
    after = report.get("user_installation_after")
    if before != after:
        errors.append("rehearsal user installation state changed")
    user_status = _validate_user_state(before)
    if user_status is None:
        errors.append("rehearsal user installation state is invalid")

    if report.get("steps") != _STEPS:
        errors.append("rehearsal steps are incomplete or reordered")
    commands = report.get("commands")
    if (
        not isinstance(commands, list)
        or len(commands) != 2
        or any(not isinstance(command, str) or not command for command in commands)
        or "-m build --no-isolation --wheel --sdist" not in commands[0]
        or "run_isolated_install_smoke.py" not in commands[1]
        or any(term in " ".join(commands).lower() for term in ("gh release", "publish", "deploy"))
    ):
        errors.append("rehearsal command inventory is invalid")

    invariants = report.get("invariants")
    expected_user_unchanged = True if user_status == "observed" else None
    expected_invariants = {
        "source_unchanged": True,
        "tag_refs_unchanged": True,
        "user_install_unchanged": expected_user_unchanged,
        "isolated_home": True,
        "artifact_bytes_unchanged": True,
    }
    if invariants != expected_invariants:
        errors.append("rehearsal invariants are invalid")
    if report.get("external_effects") != _EXTERNAL_EFFECTS:
        errors.append("rehearsal external effects are not closed")

    generated = _report_timestamp(report.get("generated_at"))
    if generated is None:
        errors.append("rehearsal generated_at is invalid")
    elif finished is not None and generated < finished:
        errors.append("rehearsal generated_at precedes the build")
    return errors


def validate_rehearsal_report(report: Mapping[str, Any]) -> None:
    errors = rehearsal_report_errors(report)
    if errors:
        raise RehearsalError("; ".join(errors))


def _hex_digest(value: object, *, lengths: tuple[int, ...] = (64,)) -> bool:
    return (
        isinstance(value, str)
        and len(value) in lengths
        and all(character in "0123456789abcdef" for character in value)
    )


def _report_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _validate_user_state(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    if value.get("status") == "not-run":
        return (
            "not-run"
            if set(value) == {"status", "reason"}
            and isinstance(value.get("reason"), str)
            and bool(value.get("reason"))
            else None
        )
    if value.get("status") != "observed" or set(value) != {
        "status",
        "kafa_version",
        "kafa_executable",
        "managed_plugin",
        "plugin_cache",
        "plugin",
    }:
        return None
    if not isinstance(value.get("kafa_version"), str) or not value.get("kafa_version"):
        return None
    executable = value.get("kafa_executable")
    managed = value.get("managed_plugin")
    cache = value.get("plugin_cache")
    plugin = value.get("plugin")
    if not all(isinstance(item, Mapping) for item in (executable, managed, cache, plugin)):
        return None
    if not _hex_digest(executable.get("sha256")):
        return None
    if not _hex_digest(managed.get("sha256")) or managed.get("sha256") != cache.get("sha256"):
        return None
    if plugin.get("installed") is not True or plugin.get("enabled") is not True:
        return None
    return "observed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build, install, and verify a Kafa release without publishing."
    )
    parser.add_argument("--repo", default=".")
    parser.add_argument("--syft", required=True)
    parser.add_argument("--codex-bin", default="")
    parser.add_argument("--user-kafa-bin", default="")
    parser.add_argument("--no-user-state-probe", action="store_true")
    parser.add_argument("--out", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    codex = args.codex_bin or shutil.which("codex")
    try:
        if not codex:
            raise RehearsalError("pinned Codex CLI is unavailable")
        report = run_release_rehearsal(
            repo,
            syft_command=[str(Path(args.syft).expanduser().resolve())],
            codex_bin=str(Path(codex).expanduser().resolve()),
            user_kafa_bin=args.user_kafa_bin,
            user_state_probe=not args.no_user_state_probe,
        )
        if args.out:
            write_report(Path(args.out).expanduser().resolve(), report)
    except (OSError, RehearsalError, SupplyChainError) as exc:
        report = {"ok": False, "error": str(exc)}

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    elif report["ok"]:
        print(
            "OK: no-publish release rehearsal passed "
            f"({report['artifact_count']} artifacts, isolated install verified)"
        )
    else:
        print(f"ERROR: {report['error']}", file=sys.stderr)
    return 0 if report["ok"] else 1


def run_release_rehearsal(
    repo: Path,
    *,
    syft_command: Sequence[str],
    codex_bin: str,
    user_kafa_bin: str = "",
    user_state_probe: bool = True,
) -> dict[str, Any]:
    repo = repo.expanduser().resolve()
    if repo.is_symlink() or not repo.is_dir():
        raise RehearsalError(f"release source is not a regular directory: {repo}")
    tooling = load_tooling(repo)
    release = release_report(repo)
    if not release["ok"]:
        failed = [check["name"] for check in release["checks"] if not check["ok"]]
        raise RehearsalError(f"release source validation failed: {failed}")
    installed = installed_build_tooling()
    expected_build = {
        "build": str(tooling["python_build"]["version"]),
        "setuptools": str(tooling["python_build"]["backend_version"]),
    }
    if installed != expected_build:
        raise RehearsalError(
            f"build tooling does not match pins: actual={installed} expected={expected_build}"
        )
    if not syft_command or not all(isinstance(item, str) and item for item in syft_command):
        raise RehearsalError("pinned Syft command is missing")
    if not codex_bin:
        raise RehearsalError("pinned Codex CLI is missing")

    source_before = source_identity(repo)
    tags_before = tag_refs_identity(repo)
    if user_state_probe:
        user_before = capture_user_state(codex_bin, user_kafa_bin)
        if user_before["status"] != "observed":
            raise RehearsalError(
                f"user installation state could not be observed: {user_before}"
            )
    else:
        user_before = {
            "status": "not-run",
            "reason": "user state probe explicitly disabled",
        }

    commands: list[str] = []
    with tempfile.TemporaryDirectory(prefix="kafa-release-rehearsal-") as temp:
        root = Path(temp)
        snapshot = root / "source"
        dist = root / "dist"
        dist.mkdir()
        copied = copy_source_snapshot(repo, snapshot)
        if (
            copied["source_tree_sha256"] != source_before["source_tree_sha256"]
            or copied["source_file_count"] != source_before["source_file_count"]
        ):
            raise RehearsalError("source snapshot does not match candidate identity")
        if source_identity(repo) != source_before:
            raise RehearsalError("source changed while creating release snapshot")

        source_date_epoch = git_source_date_epoch(repo)
        builder_command = [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--wheel",
            "--sdist",
            "--outdir",
            str(dist),
        ]
        build_env = isolated_build_env(root, source_date_epoch)
        started_at = now_iso()
        commands.append(_display_command(builder_command))
        _run_python(builder_command, cwd=snapshot, env=build_env)
        finished_at = now_iso()
        if source_identity(repo) != source_before:
            raise RehearsalError("source changed during release build")

        generated = generate_release_evidence(
            repo,
            dist,
            syft_command=syft_command,
            builder_command=builder_command,
            build_frontend_version=installed["build"],
            build_backend_version=installed["setuptools"],
            started_at=started_at,
            finished_at=finished_at,
        )
        verified_before = verify_release_evidence(repo, dist)
        if generated != verified_before:
            raise RehearsalError("generation and pre-install verification disagree")
        try:
            artifacts = subjects_by_kind(
                ArtifactSubject(
                    name=item["name"],
                    kind=item["kind"],
                    sha256=item["sha256"],
                )
                for item in verified_before["artifacts"]
            )
        except (ArtifactSubjectError, KeyError, TypeError) as exc:
            raise RehearsalError(f"verified artifact subjects are invalid: {exc}") from exc
        if set(artifacts) != {"wheel", "sdist"}:
            raise RehearsalError("verified artifact kinds are incomplete")

        smoke_script = snapshot / "tests" / "run_isolated_install_smoke.py"
        smoke_command = [
            sys.executable,
            str(smoke_script),
            "--repo",
            str(snapshot),
            "--codex-bin",
            codex_bin,
            "--wheel",
            str(dist / artifacts["wheel"].name),
            "--source-archive",
            str(dist / artifacts["sdist"].name),
            "--json",
        ]
        commands.append(_display_command(smoke_command))
        smoke = _json_output(
            _run_python(smoke_command, cwd=snapshot, env=os.environ.copy()),
            "isolated install smoke",
        )
        _validate_smoke(smoke, artifacts)

        verified_after = verify_release_evidence(repo, dist)
        if verified_after != verified_before:
            raise RehearsalError("artifact evidence changed during isolated install smoke")
        source_after = source_identity(repo)
        tags_after = tag_refs_identity(repo)
        if user_state_probe:
            user_after = capture_user_state(codex_bin, user_kafa_bin)
        else:
            user_after = dict(user_before)

    source_unchanged = source_after == source_before
    tags_unchanged = tags_after == tags_before
    user_unchanged: bool | None
    if user_state_probe:
        user_unchanged = user_after == user_before
    else:
        user_unchanged = None
    if not source_unchanged:
        raise RehearsalError("source changed during no-publish rehearsal")
    if not tags_unchanged:
        raise RehearsalError("tag refs changed during no-publish rehearsal")
    if user_state_probe and not user_unchanged:
        raise RehearsalError("user Kafa/plugin installation changed during rehearsal")

    report = {
        "ok": True,
        "report_version": REPORT_VERSION,
        "evidence_mode": "local-no-publish-rehearsal",
        "source": source_before,
        "build": {
            "frontend": installed["build"],
            "backend": installed["setuptools"],
            "python": platform.python_version(),
            "source_date_epoch": source_date_epoch,
            "started_at": started_at,
            "finished_at": finished_at,
        },
        "syft": {
            "version": tooling["sbom"]["version"],
            "commit": tooling["sbom"]["source_commit"],
            "format": tooling["sbom"]["format"],
        },
        "artifact_count": verified_after["artifact_count"],
        "sbom_count": verified_after["sbom_count"],
        "artifacts": verified_after["artifacts"],
        "supply_chain_assurance": verified_after["assurance"],
        "isolated_install": smoke,
        "user_installation_before": user_before,
        "user_installation_after": user_after,
        "steps": [
            "snapshot",
            "build",
            "generate",
            "verify-before-install",
            "isolated-install",
            "verify-after-install",
        ],
        "commands": commands,
        "invariants": {
            "source_unchanged": source_unchanged,
            "tag_refs_unchanged": tags_unchanged,
            "user_install_unchanged": user_unchanged,
            "isolated_home": True,
            "artifact_bytes_unchanged": True,
        },
        "external_effects": {
            "tag": False,
            "release": False,
            "upload": False,
            "deployment": False,
            "user_installation_change": False,
        },
        "generated_at": now_iso(),
    }
    validate_rehearsal_report(report)
    return report


def copy_source_snapshot(repo: Path, target: Path) -> dict[str, Any]:
    repo = repo.expanduser().resolve()
    target = target.expanduser().resolve()
    if target.exists():
        raise RehearsalError(f"snapshot target already exists: {target}")
    target.mkdir(parents=True)
    listed = _git(
        repo,
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
    ).split(b"\0")
    digest = hashlib.sha256(b"kafa-source-tree-v1\0")
    count = 0
    for encoded in sorted(item for item in listed if item):
        relative = encoded.decode("utf-8", "surrogateescape")
        if _ignored_source_path(relative):
            continue
        source = repo / relative
        destination = target / relative
        digest.update(encoded + b"\0")
        if not source.exists():
            digest.update(b"missing\0")
            count += 1
            continue
        if source.is_symlink() or not source.is_file():
            raise RehearsalError(f"snapshot rejects non-regular source path: {relative}")
        payload = source.read_bytes()
        executable = bool(source.stat().st_mode & stat.S_IXUSR)
        digest.update(b"executable\0" if executable else b"regular\0")
        digest.update(str(len(payload)).encode("ascii") + b"\0")
        digest.update(payload)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        destination.chmod(0o755 if executable else 0o644)
        if destination.read_bytes() != payload:
            raise RehearsalError(f"snapshot copy verification failed: {relative}")
        count += 1
    return {
        "source_tree_sha256": digest.hexdigest(),
        "source_file_count": count,
    }


def installed_build_tooling() -> dict[str, str]:
    result: dict[str, str] = {}
    for name in ("build", "setuptools"):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise RehearsalError(f"pinned build dependency is unavailable: {name}") from exc
    return result


def capture_user_state(codex_bin: str, user_kafa_bin: str = "") -> dict[str, Any]:
    kafa_bin = user_kafa_bin or shutil.which("kafa")
    if not kafa_bin:
        return {"status": "not-run", "reason": "user Kafa binary is unavailable"}
    executable = _executable_identity(Path(kafa_bin).expanduser())
    if executable is None:
        return {
            "status": "not-run",
            "reason": "user Kafa binary is not an absolute regular executable",
        }
    home_text = os.environ.get("HOME", "")
    home = Path(home_text).expanduser() if home_text else None
    if home is None or not home.is_absolute():
        return {
            "status": "not-run",
            "reason": "HOME must be a non-empty absolute path",
        }
    try:
        invocation_in_home = Path(executable["invocation_path"]).is_relative_to(home)
        resolved_in_home = Path(executable["resolved_path"]).is_relative_to(
            home.resolve(strict=True)
        )
    except OSError:
        invocation_in_home = False
        resolved_in_home = False
    if not invocation_in_home or not resolved_in_home:
        return {
            "status": "not-run",
            "reason": "user Kafa binary must be installed within HOME",
        }
    managed_plugin = home / ".agents" / "plugins" / PLUGIN_NAME
    kafa = _run_read_only([kafa_bin, "--version"])[0].strip()
    plugin_text = _run_read_only([codex_bin, "plugin", "list", "--json"])[0]
    try:
        plugin_report = json.loads(plugin_text)
        installed = plugin_report.get("installed", [])
        if not isinstance(installed, list):
            raise AttributeError("installed is not a list")
        matches = [
            item
            for item in installed
            if _is_kafa_plugin_entry(item, managed_plugin)
        ]
    except (AttributeError, json.JSONDecodeError) as exc:
        raise RehearsalError(f"invalid user plugin state: {exc}") from exc
    if len(matches) != 1:
        return {
            "status": "not-run",
            "reason": f"expected one managed user Kafa plugin, found {len(matches)}",
        }
    plugin = matches[0]
    marketplace = plugin.get("marketplaceName")
    version = plugin.get("version")
    source = plugin.get("source")
    source_path = _absolute_lexical_path(source.get("path")) if isinstance(source, dict) else None
    managed_safe = _directory_chain_is_safe(home, managed_plugin)
    managed_identity = _safe_tree_identity(managed_plugin) if managed_safe else None
    valid = (
        isinstance(marketplace, str)
        and _safe_path_component(marketplace)
        and isinstance(version, str)
        and _safe_path_component(version)
        and plugin.get("pluginId") == f"{PLUGIN_NAME}@{marketplace}"
        and plugin.get("name") == PLUGIN_NAME
        and plugin.get("installed") is True
        and plugin.get("enabled") is True
        and isinstance(source, dict)
        and source.get("source") == "local"
        and source_path == managed_plugin
        and managed_identity is not None
    )
    if not valid:
        return {
            "status": "not-run",
            "reason": "Kafa plugin is not an enabled managed user installation",
        }

    codex_home_text = os.environ.get("CODEX_HOME", "")
    codex_home = (
        Path(codex_home_text).expanduser()
        if codex_home_text
        else home / ".codex"
    )
    if not codex_home.is_absolute():
        return {
            "status": "not-run",
            "reason": "CODEX_HOME must be an absolute path",
        }
    cache_path = (
        codex_home
        / "plugins"
        / "cache"
        / marketplace
        / PLUGIN_NAME
        / version
    )
    cache_boundary = home if not codex_home_text else codex_home
    cache_safe = _directory_chain_is_safe(cache_boundary, cache_path)
    cache_identity = _safe_tree_identity(cache_path) if cache_safe else None
    if (
        cache_identity is None
        or managed_identity["sha256"] != cache_identity["sha256"]
    ):
        return {
            "status": "not-run",
            "reason": "Kafa plugin cache is missing, unsafe, or differs from managed installation",
        }
    return {
        "status": "observed",
        "kafa_version": kafa,
        "kafa_executable": executable,
        "managed_plugin": managed_identity,
        "plugin_cache": cache_identity,
        "plugin": {
            key: plugin.get(key)
            for key in (
                "pluginId",
                "name",
                "marketplaceName",
                "version",
                "installed",
                "enabled",
                "source",
                "marketplaceSource",
                "installPolicy",
                "authPolicy",
            )
        },
    }


def _is_kafa_plugin_entry(value: object, managed_plugin: Path) -> bool:
    if not isinstance(value, dict):
        return False
    plugin_id = value.get("pluginId")
    source = value.get("source")
    source_path = _absolute_lexical_path(source.get("path")) if isinstance(source, dict) else None
    return value.get("name") == PLUGIN_NAME or (
        isinstance(plugin_id, str)
        and (plugin_id == PLUGIN_NAME or plugin_id.startswith(f"{PLUGIN_NAME}@"))
    ) or source_path == managed_plugin


def _absolute_lexical_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        return None
    return Path(os.path.normpath(str(path)))


def _safe_path_component(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and Path(value).name == value
    )


def _directory_chain_is_safe(boundary: Path, target: Path) -> bool:
    if not boundary.is_absolute() or not target.is_absolute():
        return False
    try:
        relative = target.relative_to(boundary)
    except ValueError:
        return False
    current = boundary
    for part in relative.parts:
        if not current.is_dir() or path_is_link(current):
            return False
        current = current / part
    return (
        current == target
        and current.is_dir()
        and not path_is_link(current)
        and managed_tree_is_safe(current)
    )


def _safe_tree_identity(root: Path) -> dict[str, Any] | None:
    if not managed_tree_is_safe(root):
        return None
    try:
        files = [
            path
            for path in root.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        ]
    except OSError:
        return None
    digest = plugin_tree_digest(root)
    if not digest:
        return None
    return {
        "path": str(root),
        "file_count": len(files),
        "sha256": digest,
    }


def _executable_identity(path: Path) -> dict[str, Any] | None:
    if not path.is_absolute():
        return None
    invocation = Path(os.path.normpath(str(path)))
    try:
        resolved = invocation.resolve(strict=True)
        if not resolved.is_file() or path_is_link(resolved):
            return None
        payload = resolved.read_bytes()
        link_target = os.readlink(invocation) if invocation.is_symlink() else None
    except OSError:
        return None
    return {
        "invocation_path": str(invocation),
        "resolved_path": str(resolved),
        "link_target": link_target,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def tag_refs_identity(repo: Path) -> dict[str, Any]:
    completed = subprocess.run(
        ["git", "show-ref", "--tags"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if completed.returncode not in {0, 1}:
        raise RehearsalError(
            "cannot read tag refs: " + completed.stderr.decode("utf-8", "replace").strip()
        )
    lines = sorted(line for line in completed.stdout.splitlines() if line)
    payload = b"\n".join(lines) + (b"\n" if lines else b"")
    return {"count": len(lines), "sha256": hashlib.sha256(payload).hexdigest()}


def git_source_date_epoch(repo: Path) -> int:
    value = _git(repo, ["show", "-s", "--format=%ct", "HEAD"]).decode("ascii").strip()
    try:
        epoch = int(value)
    except ValueError as exc:
        raise RehearsalError(f"invalid source commit epoch: {value}") from exc
    if epoch <= 0:
        raise RehearsalError(f"invalid source commit epoch: {value}")
    return epoch


def isolated_build_env(root: Path, source_date_epoch: int) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    home = root / "builder-home"
    cache = root / "builder-cache"
    home.mkdir()
    cache.mkdir()
    env.update(
        {
            "HOME": str(home),
            "XDG_CACHE_HOME": str(cache),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONHASHSEED": "0",
            "SOURCE_DATE_EPOCH": str(source_date_epoch),
        }
    )
    return env


def _run_python(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    if not command or Path(command[0]).expanduser().resolve() != Path(sys.executable).resolve():
        raise RehearsalError("rehearsal only executes the pinned Python interpreter")
    is_build = command[1:3] == ["-m", "build"]
    is_smoke = len(command) > 1 and Path(command[1]).name == "run_isolated_install_smoke.py"
    if not (is_build or is_smoke):
        raise RehearsalError(f"rehearsal command is outside the allowlist: {command}")
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RehearsalError(
            f"rehearsal command failed ({completed.returncode}): "
            f"{(completed.stdout + completed.stderr).strip()}"
        )
    return completed.stdout


def _run_read_only(command: list[str]) -> tuple[str, str]:
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RehearsalError(
            f"read-only state command failed ({completed.returncode}): "
            f"{(completed.stdout + completed.stderr).strip()}"
        )
    return completed.stdout, completed.stderr


def _validate_smoke(
    smoke: dict[str, Any],
    artifacts: dict[str, ArtifactSubject | dict[str, Any]],
) -> None:
    required_true = {
        "ok",
        "artifact_mode",
        "marketplace_discovered",
        "plugin_enabled",
        "app_server_discovery_ok",
        "installed_quickstart_ok",
        "installed_migration_ok",
        "doctor_ok",
        "cache_hook_ok",
        "codex_unregister_ok",
        "codex_cache_removed",
        "marketplace_entry_removed",
        "managed_plugin_removed",
        "full_uninstall_ok",
        "remove_ok",
    }
    failed = sorted(name for name in required_true if smoke.get(name) is not True)
    if failed:
        raise RehearsalError(f"isolated install smoke checks failed: {failed}")
    try:
        expected = []
        for kind in ("wheel", "sdist"):
            value = artifacts[kind]
            expected.append(
                value
                if isinstance(value, ArtifactSubject)
                else ArtifactSubject(
                    name=value["name"],
                    kind=value["kind"],
                    sha256=value["sha256"],
                )
            )
        actual = [
            ArtifactSubject(
                name=smoke["wheel_name"],
                kind="wheel",
                sha256=smoke["wheel_sha256"],
            ),
            ArtifactSubject(
                name=smoke["source_archive_name"],
                kind="sdist",
                sha256=smoke["source_archive_sha256"],
            ),
        ]
        assert_exact_subjects(expected, actual)
    except (ArtifactSubjectError, KeyError, TypeError) as exc:
        raise RehearsalError(f"isolated install artifact subject mismatch: {exc}") from exc
    digest_fields = (
        "plugin_source_tree_sha256",
        "managed_plugin_tree_sha256",
        "cache_plugin_tree_sha256",
    )
    digests = [smoke.get(field) for field in digest_fields]
    if any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in digests
    ):
        raise RehearsalError("isolated install plugin digest is missing or malformed")
    if len(set(digests)) != 1:
        raise RehearsalError("isolated install plugin digest mismatch")


def _json_output(text: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RehearsalError(f"{label} returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RehearsalError(f"{label} JSON root is not an object")
    return value


def _display_command(command: Sequence[str]) -> str:
    return " ".join(Path(item).name if index == 0 else item for index, item in enumerate(command))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


if __name__ == "__main__":
    raise SystemExit(main())
