"""Build-only SBOM, checksum, and local provenance evidence for Kafa releases."""

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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


TOOLING_MANIFEST = "release-tooling.json"
RELEASE_MANIFEST = "release.json"
CHECKSUMS_FILE = "SHA256SUMS"
PROVENANCE_FILE = "kafa-build-provenance.intoto.json"
EVIDENCE_MANIFEST = "kafa-supply-chain-manifest.json"
REPORT_VERSION = "kafa-supply-chain-v1"
BUILD_TYPE = "https://kafa.local/build/python-package/v1"
BUILDER_ID = "https://kafa.local/builders/local-release-rehearsal/v1"


class SupplyChainError(RuntimeError):
    """Raised when release evidence cannot be generated or verified safely."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate or verify Kafa release supply-chain evidence."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate")
    generate.add_argument("--repo", default=".")
    generate.add_argument("--dist", required=True)
    generate.add_argument("--syft", default="")
    generate.add_argument("--started-at", default="")
    generate.add_argument("--finished-at", default="")
    generate.add_argument("--json", action="store_true")

    verify = subparsers.add_parser("verify")
    verify.add_argument("--repo", default=".")
    verify.add_argument("--dist", required=True)
    verify.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    repo = Path(args.repo).expanduser().resolve()
    dist = Path(args.dist).expanduser().resolve()
    try:
        if args.command == "generate":
            tooling = load_tooling(repo)
            syft = args.syft or shutil.which("syft")
            if not syft:
                raise SupplyChainError(
                    "pinned Syft is unavailable; setup is not-run, not a pass"
                )
            builder = [
                sys.executable,
                "-m",
                "build",
                "--no-isolation",
                "--wheel",
                "--sdist",
                "--outdir",
                str(dist),
            ]
            report = generate_release_evidence(
                repo,
                dist,
                syft_command=[syft],
                builder_command=builder,
                build_frontend_version=str(tooling["python_build"]["version"]),
                build_backend_version=_distribution_version(
                    str(tooling["python_build"]["backend"])
                ),
                started_at=args.started_at or None,
                finished_at=args.finished_at or None,
            )
        else:
            report = verify_release_evidence(repo, dist)
    except (OSError, SupplyChainError) as exc:
        report = {"ok": False, "error": str(exc)}

    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, sort_keys=True))
    elif report["ok"]:
        print(
            "OK: release supply-chain evidence verified "
            f"({report['artifact_count']} artifacts, {report['sbom_count']} SBOMs)"
        )
    else:
        print(f"ERROR: {report['error']}", file=sys.stderr)
    return 0 if report["ok"] else 1


def generate_release_evidence(
    repo: Path,
    dist: Path,
    *,
    syft_command: Sequence[str],
    builder_command: Sequence[str],
    build_frontend_version: str,
    build_backend_version: str,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> dict[str, Any]:
    repo = repo.expanduser().resolve()
    dist = dist.expanduser().resolve()
    _validate_source_and_dist(repo, dist)
    tooling = load_tooling(repo)
    artifacts = discover_artifacts(repo, dist)
    builder = _validate_builder_command(builder_command, dist)
    expected_build_version = str(tooling["python_build"]["version"])
    if build_frontend_version != expected_build_version:
        raise SupplyChainError(
            "build frontend version does not match pinned tooling: "
            f"actual={build_frontend_version} expected={expected_build_version}"
        )
    expected_backend_version = str(tooling["python_build"]["backend_version"])
    if build_backend_version != expected_backend_version:
        raise SupplyChainError(
            "build backend version does not match pinned tooling: "
            f"actual={build_backend_version} expected={expected_backend_version}"
        )
    syft = _validate_syft(syft_command, tooling)
    start = _timestamp(started_at)
    finish = _timestamp(finished_at)
    if _parse_timestamp(finish) < _parse_timestamp(start):
        raise SupplyChainError("build finished_at precedes started_at")

    source_before = source_identity(repo)
    artifact_before = {
        artifact["name"]: artifact["sha256"] for artifact in artifacts
    }
    tooling_sha256 = sha256_file(repo / TOOLING_MANIFEST)

    staged_names: list[str] = []
    with tempfile.TemporaryDirectory(
        prefix=".kafa-supply-chain-", dir=str(dist.parent)
    ) as temp:
        stage = Path(temp)
        generated_artifacts: list[dict[str, str]] = []
        for artifact in artifacts:
            artifact_path = dist / artifact["name"]
            sbom_name = f"{artifact['name']}.cdx.json"
            raw_sbom = stage / f".{sbom_name}.raw"
            command = [
                *syft_command,
                "scan",
                str(artifact_path),
                "-o",
                f"{tooling['sbom']['format']}={raw_sbom}",
            ]
            _run(command, env=_isolated_syft_env(stage))
            if sha256_file(artifact_path) != artifact["sha256"]:
                raise SupplyChainError(
                    f"artifact changed while generating SBOM: {artifact['name']}"
                )
            sbom = _load_json(raw_sbom)
            normalized = _normalize_sbom(
                sbom,
                artifact_name=artifact["name"],
                artifact_sha256=artifact["sha256"],
                syft_version=syft["version"],
            )
            sbom_path = stage / sbom_name
            _write_json(sbom_path, normalized)
            generated_artifacts.append(
                {
                    **artifact,
                    "sbom": sbom_name,
                    "sbom_sha256": sha256_file(sbom_path),
                }
            )
            staged_names.append(sbom_name)

        artifact_after = {
            artifact["name"]: sha256_file(dist / artifact["name"])
            for artifact in artifacts
        }
        if artifact_after != artifact_before:
            raise SupplyChainError("artifact bytes changed during evidence generation")
        source_after = source_identity(repo)
        if source_after != source_before:
            raise SupplyChainError("source identity changed during evidence generation")

        checksums_path = stage / CHECKSUMS_FILE
        checksums_path.write_bytes(
            b"".join(
                f"{artifact['sha256']}  {artifact['name']}\n".encode("utf-8")
                for artifact in generated_artifacts
            )
        )
        staged_names.append(CHECKSUMS_FILE)

        provenance = _provenance_statement(
            tooling=tooling,
            tooling_sha256=tooling_sha256,
            source=source_before,
            artifacts=generated_artifacts,
            builder_command=builder,
            build_frontend_version=build_frontend_version,
            build_backend_version=build_backend_version,
            syft=syft,
            started_at=start,
            finished_at=finish,
        )
        provenance_path = stage / PROVENANCE_FILE
        _write_json(provenance_path, provenance)
        staged_names.append(PROVENANCE_FILE)

        evidence_files = [
            {
                "name": name,
                "sha256": sha256_file(stage / name),
            }
            for name in sorted(staged_names)
        ]
        manifest = {
            "report_version": REPORT_VERSION,
            "assurance": tooling["local_statement_assurance"],
            "source": source_before,
            "tooling_manifest": {
                "name": TOOLING_MANIFEST,
                "sha256": tooling_sha256,
            },
            "builder": {
                "command": builder,
                "build_frontend_version": build_frontend_version,
                "build_backend_version": build_backend_version,
                "python_version": platform.python_version(),
                "syft_version": syft["version"],
                "syft_commit": syft["git_commit"],
            },
            "artifacts": generated_artifacts,
            "evidence_files": evidence_files,
            "provenance": PROVENANCE_FILE,
            "checksums": CHECKSUMS_FILE,
            "generated_at": finish,
        }
        _write_json(stage / EVIDENCE_MANIFEST, manifest)

        for name in staged_names:
            os.replace(stage / name, dist / name)
        os.replace(stage / EVIDENCE_MANIFEST, dist / EVIDENCE_MANIFEST)

    return verify_release_evidence(repo, dist)


def verify_release_evidence(repo: Path, dist: Path) -> dict[str, Any]:
    repo = repo.expanduser().resolve()
    dist = dist.expanduser().resolve()
    _validate_source_and_dist(repo, dist)
    tooling = load_tooling(repo)
    artifacts = discover_artifacts(repo, dist)
    source = source_identity(repo)
    tooling_sha256 = sha256_file(repo / TOOLING_MANIFEST)
    manifest = _load_json(dist / EVIDENCE_MANIFEST)
    _require_exact_keys(
        manifest,
        {
            "report_version",
            "assurance",
            "source",
            "tooling_manifest",
            "builder",
            "artifacts",
            "evidence_files",
            "provenance",
            "checksums",
            "generated_at",
        },
        "supply-chain manifest",
    )
    if manifest["report_version"] != REPORT_VERSION:
        raise SupplyChainError("supply-chain manifest report version mismatch")
    if manifest["assurance"] != tooling["local_statement_assurance"]:
        raise SupplyChainError("supply-chain manifest assurance mismatch")
    if manifest["source"] != source:
        raise SupplyChainError("supply-chain source identity mismatch")
    if manifest["tooling_manifest"] != {
        "name": TOOLING_MANIFEST,
        "sha256": tooling_sha256,
    }:
        raise SupplyChainError("supply-chain tooling identity mismatch")
    if manifest["checksums"] != CHECKSUMS_FILE or manifest["provenance"] != PROVENANCE_FILE:
        raise SupplyChainError("supply-chain evidence filename mismatch")
    _parse_timestamp(str(manifest["generated_at"]))

    builder = manifest["builder"]
    _require_exact_keys(
        builder,
        {
            "command",
            "build_frontend_version",
            "build_backend_version",
            "python_version",
            "syft_version",
            "syft_commit",
        },
        "supply-chain builder",
    )
    _validate_builder_command(builder["command"], dist)
    if builder["build_frontend_version"] != tooling["python_build"]["version"]:
        raise SupplyChainError("supply-chain build frontend pin mismatch")
    if builder["build_backend_version"] != tooling["python_build"]["backend_version"]:
        raise SupplyChainError("supply-chain build backend pin mismatch")
    if builder["syft_version"] != tooling["sbom"]["version"]:
        raise SupplyChainError("supply-chain Syft version pin mismatch")
    if builder["syft_commit"] != tooling["sbom"]["source_commit"]:
        raise SupplyChainError("supply-chain Syft commit pin mismatch")
    if not isinstance(builder["python_version"], str) or not builder["python_version"].strip():
        raise SupplyChainError("supply-chain Python version is missing")

    expected_subjects = {
        artifact["name"]: artifact["sha256"] for artifact in artifacts
    }
    expected_checksums = b"".join(
        f"{artifact['sha256']}  {artifact['name']}\n".encode("utf-8")
        for artifact in artifacts
    )
    checksums_path = _regular_evidence_file(dist, CHECKSUMS_FILE)
    if checksums_path.read_bytes() != expected_checksums:
        raise SupplyChainError("SHA256SUMS does not exactly match artifact bytes")

    expected_artifact_records: list[dict[str, str]] = []
    expected_byproducts: list[dict[str, Any]] = []
    for artifact in artifacts:
        sbom_name = f"{artifact['name']}.cdx.json"
        sbom_path = _regular_evidence_file(dist, sbom_name)
        sbom = _load_json(sbom_path)
        _verify_sbom(
            sbom,
            artifact_name=artifact["name"],
            artifact_sha256=artifact["sha256"],
            syft_version=str(tooling["sbom"]["version"]),
        )
        sbom_sha256 = sha256_file(sbom_path)
        expected_artifact_records.append(
            {
                **artifact,
                "sbom": sbom_name,
                "sbom_sha256": sbom_sha256,
            }
        )
        expected_byproducts.append(
            {"name": sbom_name, "digest": {"sha256": sbom_sha256}}
        )
    if manifest["artifacts"] != expected_artifact_records:
        raise SupplyChainError("supply-chain artifact/SBOM manifest mismatch")

    provenance_path = _regular_evidence_file(dist, PROVENANCE_FILE)
    provenance = _load_json(provenance_path)
    _verify_provenance(
        provenance,
        tooling=tooling,
        tooling_sha256=tooling_sha256,
        source=source,
        subjects=expected_subjects,
        byproducts=expected_byproducts,
        builder=builder,
        dist=dist,
    )

    evidence_names = [
        CHECKSUMS_FILE,
        PROVENANCE_FILE,
        *[record["sbom"] for record in expected_artifact_records],
    ]
    expected_evidence_files = [
        {"name": name, "sha256": sha256_file(_regular_evidence_file(dist, name))}
        for name in sorted(evidence_names)
    ]
    if manifest["evidence_files"] != expected_evidence_files:
        raise SupplyChainError("supply-chain evidence file digest mismatch")

    return {
        "ok": True,
        "report_version": REPORT_VERSION,
        "assurance": tooling["local_statement_assurance"],
        "source": source,
        "artifact_count": len(artifacts),
        "sbom_count": len(expected_artifact_records),
        "artifacts": expected_artifact_records,
        "checksums": CHECKSUMS_FILE,
        "provenance": PROVENANCE_FILE,
        "manifest": EVIDENCE_MANIFEST,
    }


def load_tooling(repo: Path) -> dict[str, Any]:
    tooling = _load_json(repo / TOOLING_MANIFEST)
    try:
        if tooling["schema_version"] != 1:
            raise SupplyChainError("unsupported release tooling schema")
        if tooling["local_statement_assurance"] != "unsigned-local-integrity-statement":
            raise SupplyChainError("invalid local statement assurance")
        if tooling["sbom"]["format"] != "cyclonedx-json@1.6":
            raise SupplyChainError("unsupported SBOM format pin")
        if tooling["statements"]["in_toto_type"] != "https://in-toto.io/Statement/v1":
            raise SupplyChainError("unsupported in-toto statement pin")
        if tooling["statements"]["slsa_predicate_type"] != "https://slsa.dev/provenance/v1":
            raise SupplyChainError("unsupported SLSA predicate pin")
    except (KeyError, TypeError) as exc:
        raise SupplyChainError(f"invalid release tooling manifest: {exc}") from exc
    return tooling


def discover_artifacts(repo: Path, dist: Path) -> list[dict[str, str]]:
    release = _load_json(repo / RELEASE_MANIFEST)
    pep440 = str(release.get("pep440_version", ""))
    package = str(release.get("package", ""))
    if not pep440 or package != "kafa":
        raise SupplyChainError("release manifest package/version is invalid")
    wheel_candidates = sorted(dist.glob("*.whl"))
    sdist_candidates = sorted(dist.glob("*.tar.gz"))
    if len(wheel_candidates) != 1:
        raise SupplyChainError(
            f"release evidence requires exactly one wheel, found {len(wheel_candidates)}"
        )
    if len(sdist_candidates) != 1:
        raise SupplyChainError(
            f"release evidence requires exactly one sdist, found {len(sdist_candidates)}"
        )
    expected = {
        f"kafa-{pep440}-py3-none-any.whl": "wheel",
        f"kafa-{pep440}.tar.gz": "sdist",
    }
    actual = {wheel_candidates[0].name, sdist_candidates[0].name}
    if actual != set(expected):
        raise SupplyChainError(
            f"release artifact names mismatch: actual={sorted(actual)} expected={sorted(expected)}"
        )
    records: list[dict[str, str]] = []
    for name, kind in expected.items():
        path = dist / name
        _require_regular(path, "release artifact")
        records.append({"name": name, "kind": kind, "sha256": sha256_file(path)})
    return sorted(records, key=lambda item: item["name"])


def source_identity(repo: Path) -> dict[str, Any]:
    commit = _git(repo, ["rev-parse", "HEAD"]).decode("ascii").strip()
    if not _is_hex(commit, 40):
        raise SupplyChainError("source git commit is unavailable")
    raw_status = _git(
        repo,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=no"],
    )
    status_bytes = _filtered_status(raw_status)
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
        path = repo / relative
        digest.update(encoded + b"\0")
        if not path.exists():
            digest.update(b"missing\0")
            count += 1
            continue
        if path.is_symlink() or not path.is_file():
            raise SupplyChainError(f"source identity rejects non-regular path: {relative}")
        payload = path.read_bytes()
        executable = bool(path.stat().st_mode & stat.S_IXUSR)
        digest.update((b"executable\0" if executable else b"regular\0"))
        digest.update(str(len(payload)).encode("ascii") + b"\0")
        digest.update(payload)
        count += 1
    return {
        "git_commit": commit,
        "git_status_sha256": hashlib.sha256(status_bytes).hexdigest(),
        "source_tree_sha256": digest.hexdigest(),
        "source_file_count": count,
        "dirty": bool(status_bytes),
    }


def sha256_file(path: Path) -> str:
    _require_regular(path, "hashed file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_source_and_dist(repo: Path, dist: Path) -> None:
    if repo.is_symlink() or not repo.is_dir():
        raise SupplyChainError(f"source repo is not a regular directory: {repo}")
    if dist.is_symlink() or not dist.is_dir():
        raise SupplyChainError(f"dist is not a regular directory: {dist}")
    if dist == repo or dist.is_relative_to(repo):
        raise SupplyChainError(
            "dist must be outside the source repo so evidence cannot change source identity"
        )


def _validate_syft(command: Sequence[str], tooling: dict[str, Any]) -> dict[str, str]:
    if not command or not all(isinstance(item, str) and item for item in command):
        raise SupplyChainError("Syft command is missing")
    output = _run([*command, "version", "-o", "json"], env=_isolated_syft_env())
    try:
        version = _loads_json(output)
        actual_version = str(version["version"])
        actual_commit = str(version["gitCommit"])
    except (KeyError, TypeError) as exc:
        raise SupplyChainError(f"invalid Syft version output: {exc}") from exc
    expected_version = str(tooling["sbom"]["version"])
    expected_commit = str(tooling["sbom"]["source_commit"])
    if actual_version != expected_version:
        raise SupplyChainError(
            f"Syft version does not match pinned tooling: actual={actual_version} expected={expected_version}"
        )
    if actual_commit != expected_commit:
        raise SupplyChainError(
            f"Syft commit does not match pinned tooling: actual={actual_commit} expected={expected_commit}"
        )
    return {"version": actual_version, "git_commit": actual_commit}


def _validate_builder_command(command: Sequence[str], dist: Path) -> list[str]:
    if not isinstance(command, (list, tuple)) or not all(
        isinstance(item, str) and item for item in command
    ):
        raise SupplyChainError("builder command must be a non-empty string list")
    normalized = list(command)
    expected_tail = [
        "-m",
        "build",
        "--no-isolation",
        "--wheel",
        "--sdist",
        "--outdir",
    ]
    if (
        len(normalized) != 2 + len(expected_tail)
        or normalized[1:-1] != expected_tail
        or Path(normalized[-1]).expanduser().resolve() != dist
    ):
        raise SupplyChainError(
            "builder command does not match pinned wheel+sdist build path"
        )
    executable = Path(normalized[0]).name.lower()
    if "python" not in executable and executable not in {"py", "py.exe"}:
        raise SupplyChainError("builder command does not use Python")
    normalized[-1] = str(dist)
    return normalized


def _normalize_sbom(
    sbom: dict[str, Any],
    *,
    artifact_name: str,
    artifact_sha256: str,
    syft_version: str,
) -> dict[str, Any]:
    _verify_sbom_base(
        sbom,
        artifact_name=artifact_name,
        artifact_sha256=artifact_sha256,
        syft_version=syft_version,
    )
    component = sbom["metadata"]["component"]
    component["hashes"] = [{"alg": "SHA-256", "content": artifact_sha256}]
    _verify_sbom(
        sbom,
        artifact_name=artifact_name,
        artifact_sha256=artifact_sha256,
        syft_version=syft_version,
    )
    return sbom


def _verify_sbom_base(
    sbom: dict[str, Any],
    *,
    artifact_name: str,
    artifact_sha256: str,
    syft_version: str,
) -> None:
    if sbom.get("$schema") != "http://cyclonedx.org/schema/bom-1.6.schema.json":
        raise SupplyChainError(f"SBOM schema mismatch for {artifact_name}")
    if sbom.get("bomFormat") != "CycloneDX" or sbom.get("specVersion") != "1.6":
        raise SupplyChainError(f"SBOM format mismatch for {artifact_name}")
    metadata = sbom.get("metadata")
    component = metadata.get("component") if isinstance(metadata, dict) else None
    if not isinstance(component, dict):
        raise SupplyChainError(f"SBOM subject is missing for {artifact_name}")
    if component.get("name") != artifact_name:
        raise SupplyChainError(f"SBOM subject name mismatch for {artifact_name}")
    if component.get("version") != f"sha256:{artifact_sha256}":
        raise SupplyChainError(f"SBOM subject version mismatch for {artifact_name}")
    tools = metadata.get("tools") if isinstance(metadata, dict) else None
    components = tools.get("components") if isinstance(tools, dict) else None
    if not isinstance(components, list) or not any(
        isinstance(tool, dict)
        and tool.get("name") == "syft"
        and tool.get("version") == syft_version
        for tool in components
    ):
        raise SupplyChainError(f"SBOM Syft identity mismatch for {artifact_name}")


def _verify_sbom(
    sbom: dict[str, Any],
    *,
    artifact_name: str,
    artifact_sha256: str,
    syft_version: str,
) -> None:
    _verify_sbom_base(
        sbom,
        artifact_name=artifact_name,
        artifact_sha256=artifact_sha256,
        syft_version=syft_version,
    )
    hashes = sbom["metadata"]["component"].get("hashes")
    if hashes != [{"alg": "SHA-256", "content": artifact_sha256}]:
        raise SupplyChainError(f"SBOM subject digest mismatch for {artifact_name}")


def _provenance_statement(
    *,
    tooling: dict[str, Any],
    tooling_sha256: str,
    source: dict[str, Any],
    artifacts: list[dict[str, str]],
    builder_command: list[str],
    build_frontend_version: str,
    build_backend_version: str,
    syft: dict[str, str],
    started_at: str,
    finished_at: str,
) -> dict[str, Any]:
    return {
        "_type": tooling["statements"]["in_toto_type"],
        "subject": [
            {"name": item["name"], "digest": {"sha256": item["sha256"]}}
            for item in artifacts
        ],
        "predicateType": tooling["statements"]["slsa_predicate_type"],
        "predicate": {
            "buildDefinition": {
                "buildType": BUILD_TYPE,
                "externalParameters": {"builder_command": builder_command},
                "internalParameters": {
                    "assurance": tooling["local_statement_assurance"],
                    "tooling_manifest_sha256": tooling_sha256,
                    "git_status_sha256": source["git_status_sha256"],
                    "source_file_count": source["source_file_count"],
                    "build_frontend_version": build_frontend_version,
                    "build_backend_version": build_backend_version,
                    "python_version": platform.python_version(),
                    "syft_version": syft["version"],
                    "syft_commit": syft["git_commit"],
                },
                "resolvedDependencies": [
                    {
                        "uri": f"git+local:kafa@{source['git_commit']}",
                        "digest": {
                            "gitCommit": source["git_commit"],
                            "sha256": source["source_tree_sha256"],
                        },
                    },
                    {
                        "uri": "kafa:git-status",
                        "digest": {"sha256": source["git_status_sha256"]},
                    },
                ],
            },
            "runDetails": {
                "builder": {
                    "id": BUILDER_ID,
                    "version": {
                        "build": build_frontend_version,
                        "setuptools": build_backend_version,
                        "python": platform.python_version(),
                        "syft": syft["version"],
                    },
                },
                "metadata": {
                    "invocationId": f"urn:uuid:{uuid.uuid4()}",
                    "startedOn": started_at,
                    "finishedOn": finished_at,
                },
                "byproducts": [
                    {
                        "name": item["sbom"],
                        "digest": {"sha256": item["sbom_sha256"]},
                    }
                    for item in artifacts
                ],
            },
        },
    }


def _verify_provenance(
    provenance: dict[str, Any],
    *,
    tooling: dict[str, Any],
    tooling_sha256: str,
    source: dict[str, Any],
    subjects: dict[str, str],
    byproducts: list[dict[str, Any]],
    builder: dict[str, Any],
    dist: Path,
) -> None:
    _require_exact_keys(
        provenance,
        {"_type", "subject", "predicateType", "predicate"},
        "provenance statement",
    )
    if provenance["_type"] != tooling["statements"]["in_toto_type"]:
        raise SupplyChainError("provenance in-toto type mismatch")
    if provenance["predicateType"] != tooling["statements"]["slsa_predicate_type"]:
        raise SupplyChainError("provenance predicate type mismatch")
    if _subject_map(provenance["subject"], "provenance") != subjects:
        raise SupplyChainError("provenance subjects do not exactly match artifacts")
    predicate = provenance["predicate"]
    _require_exact_keys(predicate, {"buildDefinition", "runDetails"}, "provenance predicate")
    definition = predicate["buildDefinition"]
    _require_exact_keys(
        definition,
        {"buildType", "externalParameters", "internalParameters", "resolvedDependencies"},
        "provenance build definition",
    )
    if definition["buildType"] != BUILD_TYPE:
        raise SupplyChainError("provenance build type mismatch")
    command = definition["externalParameters"].get("builder_command")
    _validate_builder_command(command, dist)
    if command != builder["command"]:
        raise SupplyChainError("provenance builder command mismatch")
    expected_internal = {
        "assurance": tooling["local_statement_assurance"],
        "tooling_manifest_sha256": tooling_sha256,
        "git_status_sha256": source["git_status_sha256"],
        "source_file_count": source["source_file_count"],
        "build_frontend_version": builder["build_frontend_version"],
        "build_backend_version": builder["build_backend_version"],
        "python_version": builder["python_version"],
        "syft_version": builder["syft_version"],
        "syft_commit": builder["syft_commit"],
    }
    if definition["internalParameters"] != expected_internal:
        raise SupplyChainError("provenance internal parameters mismatch")
    expected_dependencies = [
        {
            "uri": f"git+local:kafa@{source['git_commit']}",
            "digest": {
                "gitCommit": source["git_commit"],
                "sha256": source["source_tree_sha256"],
            },
        },
        {
            "uri": "kafa:git-status",
            "digest": {"sha256": source["git_status_sha256"]},
        },
    ]
    if definition["resolvedDependencies"] != expected_dependencies:
        raise SupplyChainError("provenance source identity mismatch")

    details = predicate["runDetails"]
    _require_exact_keys(details, {"builder", "metadata", "byproducts"}, "provenance run details")
    expected_builder = {
        "id": BUILDER_ID,
        "version": {
            "build": builder["build_frontend_version"],
            "setuptools": builder["build_backend_version"],
            "python": builder["python_version"],
            "syft": builder["syft_version"],
        },
    }
    if details["builder"] != expected_builder:
        raise SupplyChainError("provenance builder identity mismatch")
    metadata = details["metadata"]
    _require_exact_keys(
        metadata,
        {"invocationId", "startedOn", "finishedOn"},
        "provenance metadata",
    )
    if not isinstance(metadata["invocationId"], str) or not metadata["invocationId"].startswith("urn:uuid:"):
        raise SupplyChainError("provenance invocation id is invalid")
    started = _parse_timestamp(str(metadata["startedOn"]))
    finished = _parse_timestamp(str(metadata["finishedOn"]))
    if finished < started:
        raise SupplyChainError("provenance time window is invalid")
    if details["byproducts"] != byproducts:
        raise SupplyChainError("provenance SBOM byproducts mismatch")


def _subject_map(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, list):
        raise SupplyChainError(f"{label} subjects must be a list")
    result: dict[str, str] = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "digest"}:
            raise SupplyChainError(f"{label} subject shape is invalid")
        name = item["name"]
        digest = item["digest"]
        if not isinstance(name, str) or not isinstance(digest, dict) or set(digest) != {"sha256"}:
            raise SupplyChainError(f"{label} subject shape is invalid")
        sha256 = digest["sha256"]
        if not _is_hex(sha256, 64):
            raise SupplyChainError(f"{label} subject digest is invalid")
        if name in result:
            raise SupplyChainError(f"{label} contains duplicate subject: {name}")
        result[name] = sha256
    return result


def _regular_evidence_file(dist: Path, name: str) -> Path:
    if Path(name).name != name:
        raise SupplyChainError(f"unsafe evidence filename: {name}")
    path = dist / name
    _require_regular(path, "release evidence")
    return path


def _require_regular(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise SupplyChainError(f"{label} is not a regular file: {path}")


def _require_exact_keys(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        actual = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise SupplyChainError(
            f"{label} keys mismatch: actual={actual} expected={sorted(expected)}"
        )


def _load_json(path: Path) -> dict[str, Any]:
    _require_regular(path, "JSON evidence")
    try:
        value = _loads_json(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, SupplyChainError) as exc:
        raise SupplyChainError(f"invalid JSON evidence {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise SupplyChainError(f"JSON evidence root must be an object: {path.name}")
    return value


def _loads_json(text: str) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SupplyChainError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=unique_object)


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(
        (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
    )


def _run(command: Sequence[str], *, env: dict[str, str]) -> str:
    completed = subprocess.run(
        list(command),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        details = (completed.stdout + completed.stderr).strip()
        raise SupplyChainError(
            f"build-only tool failed ({completed.returncode}): {details}"
        )
    return completed.stdout


def _isolated_syft_env(root: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if root is not None:
        home = root / ".syft-home"
        cache = root / ".syft-cache"
        config = root / ".syft-config"
        home.mkdir(exist_ok=True)
        cache.mkdir(exist_ok=True)
        config.mkdir(exist_ok=True)
        env.update(
            {
                "HOME": str(home),
                "XDG_CACHE_HOME": str(cache),
                "XDG_CONFIG_HOME": str(config),
            }
        )
    env["SYFT_CHECK_FOR_APP_UPDATE"] = "false"
    env["SYFT_LOG_QUIET"] = "true"
    return env


def _git(repo: Path, args: list[str]) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SupplyChainError(
            f"git {' '.join(args)} failed: {completed.stderr.decode('utf-8', 'replace').strip()}"
        )
    return completed.stdout


def _ignored_source_path(relative: str) -> bool:
    parts = Path(relative).parts
    if any(
        part in {".git", ".ai-team", ".venv", ".ruff_cache", "__pycache__", "build", "dist"}
        or part.endswith(".egg-info")
        for part in parts
    ):
        return True
    return relative.endswith((".pyc", ".pyo"))


def _filtered_status(payload: bytes) -> bytes:
    records = payload.split(b"\0")
    output: list[bytes] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise SupplyChainError("git status porcelain record is malformed")
        status = record[:2]
        paths = [record[3:]]
        if status[:1] in {b"R", b"C"} or status[1:2] in {b"R", b"C"}:
            if index >= len(records) or not records[index]:
                raise SupplyChainError("git status rename record is incomplete")
            paths.append(records[index])
            index += 1
        decoded = [path.decode("utf-8", "surrogateescape") for path in paths]
        if all(_ignored_source_path(path) for path in decoded):
            continue
        output.append(record)
        output.extend(paths[1:])
    return b"\0".join(output) + (b"\0" if output else b"")


def _timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    parsed = _parse_timestamp(value)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SupplyChainError(f"invalid provenance timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise SupplyChainError(f"provenance timestamp lacks timezone: {value}")
    return parsed


def _is_hex(value: Any, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and all(
        character in "0123456789abcdef" for character in value
    )


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise SupplyChainError(f"pinned build dependency is unavailable: {name}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
