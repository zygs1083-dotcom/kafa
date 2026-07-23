"""Stable digest-bound summaries for volatile local evidence details."""

from __future__ import annotations

import hashlib
import json
import math
import argparse
import base64
import os
import re
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Iterator, Mapping

from .artifact_subject import (
    ArtifactSubject,
    ArtifactSubjectError,
    RegularFileSnapshot,
    manifest_records,
    parse_manifest_records,
    read_regular_file,
)
from .change_scope import (
    BLOCKING_NATIVE_PROFILES,
    BLOCKING_SCOPES,
    CHANGE_SCOPE_VERSION,
    classify_repository,
    validate_decision_report,
)


EVIDENCE_SUMMARY_VERSION = "kafa-evidence-summary-v1"
SUMMARY_KINDS = frozenset({"native-single", "native-parallel", "release-rehearsal"})
SUMMARY_STATUSES = frozenset({"passed", "failed", "blocked", "skipped", "not-run"})
SUMMARY_STATES = frozenset({"current", "historical", "unavailable", "invalid"})
SOURCE_KINDS = frozenset({"native-evaluation-source-v1", "release-source-v1"})
SCOPE_REQUIREMENTS = frozenset({"blocking", "advisory", "manual"})
CHANGE_SCOPES = frozenset(
    {
        "host",
        "packaging",
        "release-tooling",
        "native-evaluator",
        "schema-runtime",
        "docs-only",
        "unknown",
    }
)
RETENTION_CLASSES = frozenset({"repository", "ci-artifact", "local-opt-in", "none"})
EVIDENCE_ELIGIBILITY = frozenset({"historical-integrity", "current-eligible"})
_VALIDATOR_SOURCE_DIRECTORIES = (
    Path("kafa"),
    Path("plugins/codex-project-harness"),
)
_VALIDATOR_SOURCE_FILES = (
    Path("release.json"),
)
_VALIDATOR_IGNORED_DIRECTORIES = frozenset(
    {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
)
_VALIDATOR_IGNORED_SUFFIXES = frozenset({".pyc", ".pyo"})
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
OID_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
DECISION_BINDING_KEYS = frozenset(
    {
        "version",
        "state",
        "base_oid",
        "head_oid",
        "changed_paths_sha256",
        "required_profiles",
        "sha256",
    }
)


class EvidenceSummaryError(ValueError):
    """Raised when an evidence summary is ambiguous or internally inconsistent."""


def build_evidence_summary(
    *,
    detail_bytes: bytes,
    kind: str,
    source: Mapping[str, Any],
    status: str,
    binary: Iterable[ArtifactSubject],
    scope: Mapping[str, Any],
    timing: Mapping[str, Any],
    state: str,
    retention: Mapping[str, Any],
) -> dict[str, object]:
    if not isinstance(detail_bytes, bytes) or not detail_bytes:
        raise EvidenceSummaryError("detail bytes must be non-empty bytes")
    try:
        binary_records = manifest_records(binary)
    except ArtifactSubjectError as exc:
        raise EvidenceSummaryError(f"binary subjects are invalid: {exc}") from exc
    summary: dict[str, object] = {
        "report_version": EVIDENCE_SUMMARY_VERSION,
        "kind": kind,
        "source": dict(source),
        "status": status,
        "binary": binary_records,
        "scope": dict(scope),
        "timing": dict(timing),
        "digest": {
            "sha256": hashlib.sha256(detail_bytes).hexdigest(),
            "bytes": len(detail_bytes),
        },
        "state": state,
        "retention": dict(retention),
    }
    errors = validate_evidence_summary(summary)
    if errors:
        raise EvidenceSummaryError("; ".join(errors))
    return summary


def validate_evidence_summary(summary: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    expected_top = {
        "report_version",
        "kind",
        "source",
        "status",
        "binary",
        "scope",
        "timing",
        "digest",
        "state",
        "retention",
    }
    if not isinstance(summary, Mapping) or set(summary) != expected_top:
        actual = sorted(summary) if isinstance(summary, Mapping) else type(summary).__name__
        return [f"summary keys mismatch: actual={actual} expected={sorted(expected_top)}"]
    if summary.get("report_version") != EVIDENCE_SUMMARY_VERSION:
        errors.append("summary report_version is unsupported")
    kind = summary.get("kind")
    status = summary.get("status")
    state = summary.get("state")
    if kind not in SUMMARY_KINDS:
        errors.append("summary kind is unsupported")
    if status not in SUMMARY_STATUSES:
        errors.append("summary status is unsupported")
    if state not in SUMMARY_STATES:
        errors.append("summary state is unsupported")

    source = summary.get("source")
    source_clean: bool | None = None
    if not isinstance(source, Mapping) or set(source) != {
        "kind",
        "revision",
        "sha256",
        "clean",
        "status_sha256",
    }:
        errors.append("summary source shape is invalid")
    else:
        if source.get("kind") not in SOURCE_KINDS:
            errors.append("summary source kind is unsupported")
        if not _valid_oid(source.get("revision")):
            errors.append("summary source revision is invalid")
        if not _valid_sha256(source.get("sha256")):
            errors.append("summary source digest is invalid")
        source_clean = source.get("clean") if isinstance(source.get("clean"), bool) else None
        if source_clean is None:
            errors.append("summary source clean is not a boolean")
        if not _valid_sha256(source.get("status_sha256")):
            errors.append("summary source status digest is invalid")
        if source_clean is True and source.get("status_sha256") != hashlib.sha256(b"").hexdigest():
            errors.append("summary clean source has a non-empty status digest")

    binary = summary.get("binary")
    if not isinstance(binary, list):
        errors.append("summary binary is not a list")
    else:
        try:
            parse_manifest_records(binary)
        except (ArtifactSubjectError, TypeError) as exc:
            errors.append(f"summary binary subjects are invalid: {exc}")
        if status == "passed" and not binary:
            errors.append("passing summary requires binary subjects")

    scope = summary.get("scope")
    profile = None
    scope_keys = set(scope) if isinstance(scope, Mapping) else set()
    legacy_scope_keys = {"profile", "requirement", "change_scopes"}
    current_scope_keys = legacy_scope_keys | {"decision"}
    if not isinstance(scope, Mapping) or scope_keys not in (legacy_scope_keys, current_scope_keys):
        errors.append("summary scope shape is invalid")
    else:
        profile = scope.get("profile")
        expected_profile = {
            "native-single": "live-codex",
            "native-parallel": "live-codex-parallel",
            "release-rehearsal": "release-rehearsal",
        }.get(kind)
        if profile != expected_profile:
            errors.append("summary scope profile is inconsistent with kind")
        requirement = scope.get("requirement")
        if requirement not in SCOPE_REQUIREMENTS:
            errors.append("summary scope requirement is unsupported")
        change_scopes = scope.get("change_scopes")
        if not isinstance(change_scopes, list) or not change_scopes:
            errors.append("summary change scopes are invalid")
        elif any(not isinstance(value, str) or value not in CHANGE_SCOPES for value in change_scopes):
            errors.append("summary change scopes are invalid")
        elif len(change_scopes) != len(set(change_scopes)) or change_scopes != sorted(change_scopes):
            errors.append("summary change scopes are invalid")
        else:
            if set(change_scopes) & set(BLOCKING_SCOPES) and requirement != "blocking":
                errors.append("summary blocking change scope requires blocking evidence")
            if state == "current" and requirement == "manual":
                errors.append("current summary cannot use a manual scope requirement")

        decision = scope.get("decision")
        if decision is None:
            if state == "current":
                errors.append("current summary scope requires a classifier decision binding")
        elif not isinstance(decision, Mapping) or set(decision) != DECISION_BINDING_KEYS:
            errors.append("summary classifier decision binding shape is invalid")
        else:
            if decision.get("version") != CHANGE_SCOPE_VERSION:
                errors.append("summary classifier decision version is unsupported")
            if decision.get("state") not in {"classified", "unknown"}:
                errors.append("summary classifier decision state is invalid")
            for field in ("base_oid", "head_oid"):
                if not _valid_oid(decision.get(field)):
                    errors.append(f"summary classifier decision {field} is invalid")
            for field in ("changed_paths_sha256", "sha256"):
                if not _valid_sha256(decision.get(field)):
                    errors.append(f"summary classifier decision {field} is invalid")
            profiles = decision.get("required_profiles")
            if not isinstance(profiles, list) or any(
                not isinstance(value, str) for value in profiles
            ) or profiles != sorted(set(profiles)):
                errors.append("summary classifier decision profiles are invalid")
            elif requirement == "blocking" and tuple(profiles) != BLOCKING_NATIVE_PROFILES:
                errors.append("summary blocking decision omitted required Native profiles")
            elif requirement == "advisory" and profiles:
                errors.append("summary advisory decision unexpectedly requires Native profiles")
            if decision.get("state") == "unknown":
                if change_scopes != ["unknown"] or requirement != "blocking":
                    errors.append("summary unknown decision must remain blocking")
            if isinstance(source, Mapping) and (
                source.get("revision") != decision.get("head_oid")
            ):
                errors.append("summary source revision does not match classifier decision head")

    timing = summary.get("timing")
    if not isinstance(timing, Mapping) or set(timing) != {"generated_at", "duration_seconds"}:
        errors.append("summary timing shape is invalid")
    else:
        if not _valid_timestamp(timing.get("generated_at")):
            errors.append("summary generated_at is invalid")
        duration = timing.get("duration_seconds")
        if duration is not None and (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not math.isfinite(float(duration))
            or duration < 0
        ):
            errors.append("summary duration_seconds is invalid")

    digest = summary.get("digest")
    if not isinstance(digest, Mapping) or set(digest) != {"sha256", "bytes"}:
        errors.append("summary detail digest shape is invalid")
    else:
        if not _valid_sha256(digest.get("sha256")):
            errors.append("summary detail digest is invalid")
        size = digest.get("bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            errors.append("summary detail byte count is invalid")

    retention = summary.get("retention")
    if not isinstance(retention, Mapping) or set(retention) != {"class", "locator", "days"}:
        errors.append("summary retention shape is invalid")
    else:
        retention_class = retention.get("class")
        locator = retention.get("locator")
        days = retention.get("days")
        if retention_class not in RETENTION_CLASSES:
            errors.append("summary retention class is unsupported")
        elif retention_class == "none":
            if locator is not None or days is not None:
                errors.append("summary none retention must not have a locator or days")
        elif retention_class == "ci-artifact":
            if not isinstance(locator, str) or not locator.strip() or not isinstance(days, int) or isinstance(days, bool) or days <= 0:
                errors.append("summary ci-artifact retention is invalid")
        elif retention_class == "repository":
            if not _safe_repository_locator(locator) or days is not None:
                errors.append("summary repository retention is invalid")
        elif retention_class == "local-opt-in":
            if not isinstance(locator, str) or not locator.strip() or days is not None:
                errors.append("summary local-opt-in retention is invalid")

    if state == "current" and (status != "passed" or source_clean is not True):
        errors.append("summary current state requires passed status and clean source")
    if state == "unavailable" and status not in {"blocked", "skipped", "not-run"}:
        errors.append("summary unavailable state requires blocked, skipped, or not-run status")
    if state == "invalid" and status != "failed":
        errors.append("summary invalid state requires failed status")
    if status == "passed" and state not in {"current", "historical"}:
        errors.append("passing summary must be current or historical")
    return errors


def summary_json_bytes(summary: Mapping[str, Any]) -> bytes:
    errors = validate_evidence_summary(summary)
    if errors:
        raise EvidenceSummaryError("; ".join(errors))
    try:
        return (json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvidenceSummaryError(f"summary is not canonical JSON: {exc}") from exc


def _decision_binding(
    report: Mapping[str, Any],
    decision_bytes: bytes,
    *,
    expected_sha256: str | None = None,
    validator_repo: Path | None = None,
    require_repository_binding: bool = False,
) -> dict[str, object]:
    errors = validate_decision_report(report)
    actual_sha256 = hashlib.sha256(decision_bytes).hexdigest()
    if expected_sha256 is not None and expected_sha256 != actual_sha256:
        errors.append("classifier decision digest does not match the trusted expected digest")
    if require_repository_binding:
        if expected_sha256 is None:
            errors.append("current classifier decision requires a trusted expected digest")
        errors.extend(_repository_decision_errors(report, validator_repo))
    if errors:
        raise EvidenceSummaryError("classifier decision is invalid: " + "; ".join(errors))
    return {
        "version": report["version"],
        "state": report["state"],
        "base_oid": report["base_oid"],
        "head_oid": report["head_oid"],
        "changed_paths_sha256": report["changed_paths_sha256"],
        "required_profiles": list(report["required_profiles"]),
        "sha256": actual_sha256,
    }


def _repository_decision_errors(
    report: Mapping[str, Any],
    validator_repo: Path | None,
) -> list[str]:
    if validator_repo is None:
        return ["current classifier decision repository is unavailable"]
    repo = validator_repo.expanduser().resolve()
    if not repo.is_dir():
        return [f"current classifier decision repository is unavailable: {repo}"]
    base_oid = report.get("base_oid")
    head_oid = report.get("head_oid")
    if not isinstance(base_oid, str) or not isinstance(head_oid, str):
        return ["current classifier decision object IDs are invalid"]
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("GIT_") and name not in {"GIT_SSH", "GIT_SSH_COMMAND"}:
            environment.pop(name, None)
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    try:
        current_head = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=repo,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        ).stdout.strip()
        status = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignored=no",
            ],
            cwd=repo,
            env=environment,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        return [f"current classifier decision repository cannot be inspected: {exc}"]
    errors: list[str] = []
    if current_head != head_oid:
        errors.append("classifier decision head does not match current repository HEAD")
    if status:
        errors.append("current classifier decision repository is not clean")
    reproduced = classify_repository(repo, base_oid=base_oid, head_oid=head_oid).to_dict()
    if reproduced != dict(report):
        errors.append(
            "classifier decision does not match the repository base/head/path comparison"
        )
    return errors


def _scope_from_decision_or_legacy(
    *,
    profile: str,
    state: str,
    decision_bytes: bytes | None,
    expected_decision_sha256: str | None,
    validator_repo: Path | None,
    requirement: str | None,
    change_scopes: Iterable[str] | None,
    allow_legacy_historical_scope: bool,
) -> dict[str, object]:
    if decision_bytes is not None:
        report = _load_detail(decision_bytes)
        binding = _decision_binding(
            report,
            decision_bytes,
            expected_sha256=expected_decision_sha256,
            validator_repo=validator_repo,
            require_repository_binding=state == "current",
        )
        return {
            "profile": profile,
            "requirement": report["native_requirement"],
            "change_scopes": sorted(report["scopes"]),
            "decision": binding,
        }
    if state == "current":
        raise EvidenceSummaryError("current evidence requires a classifier decision binding")
    if not allow_legacy_historical_scope:
        raise EvidenceSummaryError(
            "new evidence requires a classifier decision; legacy historical scope is import-only"
        )
    if requirement is None or change_scopes is None:
        raise EvidenceSummaryError("legacy historical evidence requires an explicit scope")
    return {
        "profile": profile,
        "requirement": requirement,
        "change_scopes": sorted(set(change_scopes)),
    }


def summarize_native_detail(
    detail_bytes: bytes,
    *,
    decision_bytes: bytes | None = None,
    expected_decision_sha256: str | None = None,
    requirement: str | None = None,
    change_scopes: Iterable[str] | None = None,
    eligibility: str = "historical-integrity",
    validator_repo: Path | None = None,
    allow_legacy_historical_scope: bool = False,
    retention: Mapping[str, Any],
) -> dict[str, object]:
    detail = _load_detail(detail_bytes)
    validator_errors = _native_validator_errors(
        detail_bytes,
        eligibility=eligibility,
        validator_repo=validator_repo,
    )
    if validator_errors:
        raise EvidenceSummaryError(
            "Native detail semantic validation failed: " + "; ".join(validator_errors)
        )
    mode = detail.get("mode")
    kind = {
        "live-codex": "native-single",
        "live-codex-parallel": "native-parallel",
    }.get(mode)
    if kind is None:
        raise EvidenceSummaryError("Native detail mode is unsupported")
    identity = detail.get("evaluation_source")
    if not isinstance(identity, Mapping):
        raise EvidenceSummaryError("Native detail source is missing")
    try:
        clean = not _require_bool(identity["git_dirty"], "Native git_dirty")
        source = {
            "kind": "native-evaluation-source-v1",
            "revision": _require_str(identity["git_head"], "Native git_head"),
            "sha256": _require_str(identity["workspace_sha256"], "Native workspace_sha256"),
            "clean": clean,
            "status_sha256": _require_str(identity["status_sha256"], "Native status_sha256"),
        }
        generated_at = _require_str(identity["generated_at"], "Native generated_at")
    except KeyError as exc:
        raise EvidenceSummaryError(f"Native detail source is incomplete: {exc}") from exc
    status = detail.get("live_status")
    if status not in SUMMARY_STATUSES:
        raise EvidenceSummaryError("Native detail status is unsupported")
    binary: list[ArtifactSubject] = []
    native_host = detail.get("native_host")
    if isinstance(native_host, Mapping):
        resolved_path = _require_str(native_host.get("resolved_path"), "Native binary path")
        name = PureWindowsPath(resolved_path).name if "\\" in resolved_path else PurePosixPath(resolved_path).name
        try:
            binary.append(
                ArtifactSubject(
                    name=name,
                    kind="codex-cli",
                    sha256=_require_str(native_host.get("sha256"), "Native binary digest"),
                )
            )
        except ArtifactSubjectError as exc:
            raise EvidenceSummaryError(f"Native binary subject is invalid: {exc}") from exc
    report_summary = detail.get("summary")
    if not isinstance(report_summary, Mapping):
        raise EvidenceSummaryError("Native detail summary is missing")
    duration = report_summary.get("duration_seconds")
    state = (
        "current"
        if status == "passed" and clean and eligibility == "current-eligible"
        else "historical"
        if status == "passed"
        else "invalid"
        if status == "failed"
        else "unavailable"
    )
    scope = _scope_from_decision_or_legacy(
        profile=mode,
        state=state,
        decision_bytes=decision_bytes,
        expected_decision_sha256=expected_decision_sha256,
        validator_repo=validator_repo,
        requirement=requirement,
        change_scopes=change_scopes,
        allow_legacy_historical_scope=allow_legacy_historical_scope,
    )
    return build_evidence_summary(
        detail_bytes=detail_bytes,
        kind=kind,
        source=source,
        status=status,
        binary=binary,
        scope=scope,
        timing={"generated_at": generated_at, "duration_seconds": duration},
        state=state,
        retention=retention,
    )


def summarize_rehearsal_detail(
    detail_bytes: bytes,
    *,
    decision_bytes: bytes | None = None,
    expected_decision_sha256: str | None = None,
    requirement: str | None = None,
    change_scopes: Iterable[str] | None = None,
    eligibility: str = "historical-integrity",
    validator_repo: Path | None = None,
    allow_legacy_historical_scope: bool = False,
    retention: Mapping[str, Any],
) -> dict[str, object]:
    if eligibility not in EVIDENCE_ELIGIBILITY:
        raise EvidenceSummaryError(f"unsupported evidence eligibility: {eligibility}")
    detail = _load_detail(detail_bytes)
    semantic_errors = _rehearsal_detail_semantic_errors(detail)
    if semantic_errors:
        raise EvidenceSummaryError(
            "rehearsal detail semantic validation failed: "
            + "; ".join(semantic_errors)
        )
    if detail.get("report_version") != "kafa-release-rehearsal-v1":
        raise EvidenceSummaryError("rehearsal detail version is unsupported")
    source_detail = detail.get("source")
    if not isinstance(source_detail, Mapping):
        raise EvidenceSummaryError("rehearsal detail source is missing")
    try:
        clean = not _require_bool(source_detail["dirty"], "rehearsal dirty")
        source = {
            "kind": "release-source-v1",
            "revision": _require_str(source_detail["git_commit"], "rehearsal git_commit"),
            "sha256": _require_str(source_detail["source_tree_sha256"], "rehearsal source digest"),
            "clean": clean,
            "status_sha256": _require_str(source_detail["git_status_sha256"], "rehearsal status digest"),
        }
    except KeyError as exc:
        raise EvidenceSummaryError(f"rehearsal detail source is incomplete: {exc}") from exc
    if detail.get("ok") is not True:
        raise EvidenceSummaryError("only a complete rehearsal detail can be summarized")
    artifact_records = detail.get("artifacts")
    if not isinstance(artifact_records, list):
        raise EvidenceSummaryError("rehearsal artifact subjects are missing")
    try:
        binary = [
            ArtifactSubject(
                name=_require_str(record["name"], "rehearsal artifact name"),
                kind=_require_str(record["kind"], "rehearsal artifact kind"),
                sha256=_require_str(record["sha256"], "rehearsal artifact digest"),
            )
            for record in artifact_records
            if isinstance(record, Mapping)
        ]
    except (ArtifactSubjectError, KeyError) as exc:
        raise EvidenceSummaryError(f"rehearsal artifact subject is invalid: {exc}") from exc
    if len(binary) != len(artifact_records):
        raise EvidenceSummaryError("rehearsal artifact subject shape is invalid")
    generated_at = _require_str(detail.get("generated_at"), "rehearsal generated_at")
    duration: float | None = None
    build = detail.get("build")
    if isinstance(build, Mapping) and isinstance(build.get("started_at"), str) and isinstance(
        build.get("finished_at"), str
    ):
        started = _parse_timestamp(build["started_at"])
        finished = _parse_timestamp(build["finished_at"])
        duration = round((finished - started).total_seconds(), 6)
        if duration < 0:
            raise EvidenceSummaryError("rehearsal timing is reversed")
    state = (
        "current"
        if clean and eligibility == "current-eligible"
        else "historical"
    )
    scope = _scope_from_decision_or_legacy(
        profile="release-rehearsal",
        state=state,
        decision_bytes=decision_bytes,
        expected_decision_sha256=expected_decision_sha256,
        validator_repo=validator_repo,
        requirement=requirement,
        change_scopes=change_scopes,
        allow_legacy_historical_scope=allow_legacy_historical_scope,
    )
    return build_evidence_summary(
        detail_bytes=detail_bytes,
        kind="release-rehearsal",
        source=source,
        status="passed",
        binary=binary,
        scope=scope,
        timing={"generated_at": generated_at, "duration_seconds": duration},
        state=state,
        retention=retention,
    )


def summary_detail_errors(
    summary: Mapping[str, Any],
    detail_path: Path,
    *,
    eligibility: str = "historical-integrity",
    validator_repo: Path | None = None,
    decision_path: Path | None = None,
    expected_decision_sha256: str | None = None,
) -> list[str]:
    errors = validate_evidence_summary(summary)
    if errors:
        return errors
    try:
        detail_bytes = read_regular_detail(detail_path)
    except EvidenceSummaryError as exc:
        return [str(exc)]
    digest = summary["digest"]
    actual_sha256 = hashlib.sha256(detail_bytes).hexdigest()
    if digest.get("sha256") != actual_sha256 or digest.get("bytes") != len(detail_bytes):
        return ["evidence detail digest or byte count does not match summary"]
    scope = summary["scope"]
    retention = summary["retention"]
    decision_bytes: bytes | None = None
    decision = scope.get("decision") if isinstance(scope, Mapping) else None
    if decision is not None:
        if decision_path is None:
            if eligibility == "current-eligible":
                errors.append("classifier decision detail is unavailable for current evidence")
        else:
            try:
                decision_bytes = read_regular_detail(decision_path)
                report = _load_detail(decision_bytes)
                decision_errors = validate_decision_report(report)
                if decision_errors:
                    errors.extend(f"classifier decision is invalid: {error}" for error in decision_errors)
                binding = _decision_binding(
                    report,
                    decision_bytes,
                    expected_sha256=expected_decision_sha256,
                    validator_repo=validator_repo,
                    require_repository_binding=eligibility == "current-eligible",
                )
                if binding != dict(decision):
                    errors.append("classifier decision detail does not match summary binding")
                if report.get("native_requirement") != scope.get("requirement"):
                    errors.append("classifier decision requirement does not match summary scope")
                if sorted(report.get("scopes", ())) != scope.get("change_scopes"):
                    errors.append("classifier decision scopes do not match summary scope")
            except EvidenceSummaryError as exc:
                errors.append(str(exc))
    try:
        if summary["kind"] in {"native-single", "native-parallel"}:
            rebuilt = summarize_native_detail(
                detail_bytes,
                decision_bytes=decision_bytes,
                expected_decision_sha256=expected_decision_sha256,
                requirement=scope["requirement"],
                change_scopes=scope["change_scopes"],
                eligibility=eligibility,
                validator_repo=validator_repo,
                allow_legacy_historical_scope=(
                    decision is None and eligibility == "historical-integrity"
                ),
                retention=retention,
            )
        elif summary["kind"] == "release-rehearsal":
            rebuilt = summarize_rehearsal_detail(
                detail_bytes,
                decision_bytes=decision_bytes,
                expected_decision_sha256=expected_decision_sha256,
                requirement=scope["requirement"],
                change_scopes=scope["change_scopes"],
                eligibility=eligibility,
                validator_repo=validator_repo,
                allow_legacy_historical_scope=(
                    decision is None and eligibility == "historical-integrity"
                ),
                retention=retention,
            )
        else:
            return ["summary kind cannot select a detail validator"]
    except EvidenceSummaryError as exc:
        return [f"evidence detail is stale, substituted, or invalid: {exc}"]
    if rebuilt != dict(summary):
        errors.append("evidence detail is stale or inconsistent with summary fields")
    if eligibility == "current-eligible":
        if summary.get("state") != "current":
            errors.append(f"evidence state is {summary.get('state')}, not current")
        if summary.get("status") != "passed":
            errors.append(f"evidence status is {summary.get('status')}, not passed")
    elif eligibility != "historical-integrity":
        errors.append(f"unsupported evidence eligibility: {eligibility}")
    return errors


def _native_validator_errors(
    detail_bytes: bytes,
    *,
    eligibility: str,
    validator_repo: Path | None,
) -> list[str]:
    if eligibility not in EVIDENCE_ELIGIBILITY:
        return [f"unsupported evidence eligibility: {eligibility}"]
    if validator_repo is None:
        return ["Native detail validator repository is unavailable"]
    repo = validator_repo.expanduser().resolve()
    try:
        with _native_validator_source(repo, eligibility=eligibility) as snapshot_repo:
            script = (
                snapshot_repo
                / "plugins"
                / "codex-project-harness"
                / "scripts"
                / "run_agent_e2e_eval.py"
            )
            script_snapshot = read_regular_file(script)
            bootstrap_payload = json.dumps(
                {
                    "script_path": str(script),
                    "script_base64": base64.b64encode(script_snapshot.payload).decode("ascii"),
                    "detail_base64": base64.b64encode(detail_bytes).decode("ascii"),
                    "eligibility": eligibility,
                },
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            env = {
                name: value
                for name, value in os.environ.items()
                if name != "PYTHONPATH" and not name.upper().startswith("GIT_")
            }
            env.update(
                {
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_NO_LAZY_FETCH": "1",
                    "GIT_NO_REPLACE_OBJECTS": "1",
                    "GIT_OPTIONAL_LOCKS": "0",
                    "GIT_TERMINAL_PROMPT": "0",
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    _NATIVE_VALIDATOR_BOOTSTRAP,
                ],
                cwd=snapshot_repo,
                env=env,
                input=bootstrap_payload,
                capture_output=True,
                check=False,
                timeout=120,
            )
            if not script_snapshot.matches_path(script):
                return ["Native detail validator changed while running"]
            response = _load_detail(completed.stdout)
    except (ArtifactSubjectError, EvidenceSummaryError) as exc:
        return [f"Native detail validator is unavailable: {exc}"]
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"Native detail validator could not run: {exc}"]
    except (UnicodeError, ValueError) as exc:
        return [f"Native detail validator setup is invalid: {exc}"]
    if not isinstance(response, Mapping):
        return ["Native detail validator response is not an object"]
    if set(response) != {"ok", "eligibility", "errors"}:
        return ["Native detail validator response shape is invalid"]
    raw_errors = response.get("errors")
    if (
        response.get("eligibility") != eligibility
        or not isinstance(response.get("ok"), bool)
        or not isinstance(raw_errors, list)
        or any(not isinstance(error, str) or not error for error in raw_errors)
        or response.get("ok") is not (not raw_errors)
        or completed.returncode != (0 if not raw_errors else 1)
    ):
        return ["Native detail validator response is inconsistent"]
    return list(raw_errors)


@contextmanager
def _native_validator_source(
    repo: Path,
    *,
    eligibility: str,
) -> Iterator[Path]:
    """Yield a private source tree so the validator and imports share one snapshot."""

    with tempfile.TemporaryDirectory(prefix="kafa-native-validator-") as temp:
        snapshot_repo = Path(temp) / "repository"
        if eligibility == "current-eligible":
            source_head = _snapshot_current_validator_source(repo, snapshot_repo)
        else:
            source_head = None
            snapshot_repo.mkdir(mode=0o700)
            _snapshot_historical_validator_source(repo, snapshot_repo)
        with _sealed_validator_source(snapshot_repo):
            yield snapshot_repo
        if source_head is not None:
            _assert_current_validator_repository(repo, expected_head=source_head)


def _validator_git_environment() -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _snapshot_current_validator_source(repo: Path, snapshot_repo: Path) -> str:
    """Clone the clean committed HEAD without sharing mutable object storage."""

    if not repo.is_dir():
        raise EvidenceSummaryError(f"current validator repository is unavailable: {repo}")
    head = _assert_current_validator_repository(repo)
    environment = _validator_git_environment()
    try:
        subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--no-local",
                "--no-checkout",
                "--no-tags",
                "--",
                str(repo),
                str(snapshot_repo),
            ],
            env=environment,
            capture_output=True,
            check=True,
            timeout=120,
        )
        subprocess.run(
            ["git", "config", "core.autocrlf", "false"],
            cwd=snapshot_repo,
            env=environment,
            capture_output=True,
            check=True,
            timeout=15,
        )
        subprocess.run(
            ["git", "config", "core.filemode", "false" if os.name == "nt" else "true"],
            cwd=snapshot_repo,
            env=environment,
            capture_output=True,
            check=True,
            timeout=15,
        )
        subprocess.run(
            ["git", "checkout", "--quiet", "--detach", head],
            cwd=snapshot_repo,
            env=environment,
            capture_output=True,
            check=True,
            timeout=120,
        )
        snapshot_head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=snapshot_repo,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        ).stdout.strip()
        snapshot_status = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignored=no",
            ],
            cwd=snapshot_repo,
            env=environment,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise EvidenceSummaryError(f"current validator snapshot failed: {exc}") from exc
    if snapshot_head != head or snapshot_status:
        raise EvidenceSummaryError("current validator snapshot is not the clean source HEAD")
    _validator_source_files(snapshot_repo)
    return head


def _assert_current_validator_repository(
    repo: Path,
    *,
    expected_head: str | None = None,
) -> str:
    environment = _validator_git_environment()
    try:
        head = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=repo,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        ).stdout.strip()
        status = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignored=no",
            ],
            cwd=repo,
            env=environment,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise EvidenceSummaryError(
            f"current validator repository cannot be inspected: {exc}"
        ) from exc
    if expected_head is not None and head != expected_head:
        raise EvidenceSummaryError("current validator repository HEAD changed while validating")
    if status:
        raise EvidenceSummaryError("current validator repository is not clean")
    return head


def _snapshot_historical_validator_source(repo: Path, snapshot_repo: Path) -> None:
    """Descriptor-copy the validator import roots and verify the source set stayed fixed."""

    if not repo.is_dir():
        raise EvidenceSummaryError(f"historical validator repository is unavailable: {repo}")
    before = _validator_source_files(repo)
    snapshots: dict[Path, RegularFileSnapshot] = {}
    for relative in before:
        source = repo / relative
        try:
            captured = read_regular_file(source)
        except ArtifactSubjectError as exc:
            raise EvidenceSummaryError(str(exc)) from exc
        destination = snapshot_repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            destination.write_bytes(captured.payload)
            os.chmod(destination, stat.S_IMODE(captured.mode))
        except OSError as exc:
            raise EvidenceSummaryError(
                f"historical validator snapshot cannot write {relative.as_posix()}: {exc}"
            ) from exc
        snapshots[relative] = captured
    after = _validator_source_files(repo)
    if after != before:
        raise EvidenceSummaryError("historical validator source set changed while snapshotting")
    changed = [
        relative.as_posix()
        for relative, captured in snapshots.items()
        if not captured.matches_path(repo / relative)
    ]
    if changed:
        raise EvidenceSummaryError(
            "historical validator source changed while snapshotting: " + ",".join(changed)
        )


def _validator_source_files(repo: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for relative in _VALIDATOR_SOURCE_FILES:
        path = repo / relative
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise EvidenceSummaryError(
                f"validator source cannot be inspected: {relative.as_posix()}: {exc}"
            ) from exc
        if _unsafe_snapshot_metadata(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise EvidenceSummaryError(
                f"validator source is not a regular file: {relative.as_posix()}"
            )
        files.append(relative)
    for relative in _VALIDATOR_SOURCE_DIRECTORIES:
        root = repo / relative
        try:
            metadata = os.lstat(root)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise EvidenceSummaryError(
                f"validator source cannot be inspected: {relative.as_posix()}: {exc}"
            ) from exc
        if _unsafe_snapshot_metadata(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise EvidenceSummaryError(
                f"validator source is not a directory: {relative.as_posix()}"
            )
        pending = [relative]
        while pending:
            directory_relative = pending.pop()
            directory = repo / directory_relative
            try:
                with os.scandir(directory) as entries:
                    children = sorted(entries, key=lambda entry: entry.name)
            except OSError as exc:
                raise EvidenceSummaryError(
                    "historical validator directory cannot be scanned: "
                    f"{directory_relative.as_posix()}: {exc}"
                ) from exc
            for entry in children:
                child_relative = directory_relative / entry.name
                if entry.name in _VALIDATOR_IGNORED_DIRECTORIES:
                    continue
                if Path(entry.name).suffix in _VALIDATOR_IGNORED_SUFFIXES:
                    continue
                try:
                    child_metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise EvidenceSummaryError(
                        "validator source cannot be inspected: "
                        f"{child_relative.as_posix()}: {exc}"
                    ) from exc
                if _unsafe_snapshot_metadata(child_metadata):
                    raise EvidenceSummaryError(
                        "validator source contains a link or reparse point: "
                        + child_relative.as_posix()
                    )
                if stat.S_ISDIR(child_metadata.st_mode):
                    pending.append(child_relative)
                elif stat.S_ISREG(child_metadata.st_mode):
                    files.append(child_relative)
                else:
                    raise EvidenceSummaryError(
                        "validator source contains a special file: "
                        + child_relative.as_posix()
                    )
    return tuple(sorted(files, key=lambda path: path.as_posix()))


@contextmanager
def _sealed_validator_source(repo: Path) -> Iterator[None]:
    """Make the private source read-only and detect transient replace-and-restore."""

    files = _validator_source_files(repo)
    directories: set[Path] = {Path()}
    for relative in files:
        parent = relative.parent
        while parent != Path():
            directories.add(parent)
            parent = parent.parent
    ordered_directories = tuple(
        sorted(directories, key=lambda path: (len(path.parts), path.as_posix()))
    )
    original_modes: dict[Path, int] = {}
    for relative in (*files, *ordered_directories):
        path = repo / relative
        try:
            metadata = os.lstat(path)
        except OSError as exc:
            raise EvidenceSummaryError(
                f"validator snapshot path cannot be sealed: {relative.as_posix()}: {exc}"
            ) from exc
        expected_kind = stat.S_ISREG if relative in files else stat.S_ISDIR
        if _unsafe_snapshot_metadata(metadata) or not expected_kind(metadata.st_mode):
            raise EvidenceSummaryError(
                f"validator snapshot path changed before sealing: {relative.as_posix()}"
            )
        original_modes[relative] = stat.S_IMODE(metadata.st_mode)
    sealed_files: dict[Path, RegularFileSnapshot] | None = None
    sealed_directories: dict[
        Path, tuple[int, int, int, int, int, int]
    ] | None = None
    try:
        for relative in files:
            mode = original_modes[relative] & ~0o222
            os.chmod(repo / relative, mode)
        for relative in reversed(ordered_directories):
            mode = original_modes[relative] & ~0o222
            os.chmod(repo / relative, mode)
        sealed_files = {
            relative: read_regular_file(repo / relative)
            for relative in files
        }
        sealed_directories = {
            relative: _snapshot_metadata_identity(os.lstat(repo / relative))
            for relative in ordered_directories
        }
        if _validator_source_files(repo) != files:
            raise EvidenceSummaryError("validator snapshot source set changed while sealing")
        yield
        changed = _sealed_validator_source_changes(
            repo,
            files=files,
            captured_files=sealed_files,
            directories=ordered_directories,
            captured_directories=sealed_directories,
        )
        if changed:
            raise EvidenceSummaryError(
                "validator snapshot changed while running: " + ",".join(changed)
            )
    except ArtifactSubjectError as exc:
        raise EvidenceSummaryError(f"validator snapshot cannot be sealed: {exc}") from exc
    except OSError as exc:
        raise EvidenceSummaryError(f"validator snapshot sealing failed: {exc}") from exc
    finally:
        restore_errors: list[str] = []
        safe_to_restore = (
            sealed_files is not None
            and sealed_directories is not None
            and not _sealed_validator_source_changes(
                repo,
                files=files,
                captured_files=sealed_files,
                directories=ordered_directories,
                captured_directories=sealed_directories,
            )
        )
        if safe_to_restore:
            for relative in ordered_directories:
                try:
                    os.chmod(repo / relative, original_modes[relative])
                except OSError as exc:
                    restore_errors.append(f"{relative.as_posix()}: {exc}")
            for relative in files:
                try:
                    os.chmod(repo / relative, original_modes[relative])
                except OSError as exc:
                    restore_errors.append(f"{relative.as_posix()}: {exc}")
        if restore_errors:
            raise EvidenceSummaryError(
                "validator snapshot permissions could not be restored: "
                + "; ".join(restore_errors)
            )


def _sealed_validator_source_changes(
    repo: Path,
    *,
    files: tuple[Path, ...],
    captured_files: Mapping[Path, RegularFileSnapshot],
    directories: tuple[Path, ...],
    captured_directories: Mapping[Path, tuple[int, int, int, int, int, int]],
) -> list[str]:
    for relative in directories:
        if (
            _snapshot_directory_identity(repo / relative)
            != captured_directories.get(relative)
        ):
            return [relative.as_posix() or "."]
    changed_files = [
        relative.as_posix()
        for relative, captured in captured_files.items()
        if not captured.matches_path(repo / relative)
    ]
    if changed_files:
        return sorted(changed_files)
    try:
        if _validator_source_files(repo) != files:
            return ["source-set"]
    except EvidenceSummaryError:
        return ["source-set"]
    return []


def _snapshot_directory_identity(path: Path) -> tuple[int, int, int, int, int, int] | None:
    try:
        metadata = os.lstat(path)
    except OSError:
        return None
    if _unsafe_snapshot_metadata(metadata) or not stat.S_ISDIR(metadata.st_mode):
        return None
    return _snapshot_metadata_identity(metadata)


def _snapshot_metadata_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _unsafe_snapshot_metadata(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(reparse_flag and file_attributes & reparse_flag)


_NATIVE_VALIDATOR_BOOTSTRAP = r"""
import base64
import json
import sys


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate bootstrap JSON key: " + key)
        result[key] = value
    return result


payload = {}
try:
    payload = json.loads(
        sys.stdin.buffer.read().decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError("non-finite bootstrap JSON value: " + value)
        ),
    )
    script_path = payload["script_path"]
    script_bytes = base64.b64decode(payload["script_base64"], validate=True)
    detail_bytes = base64.b64decode(payload["detail_base64"], validate=True)
    eligibility = payload["eligibility"]
    namespace = {
        "__name__": "_kafa_native_evidence_validator",
        "__file__": script_path,
        "__package__": None,
    }
    code = compile(script_bytes, script_path, "exec", dont_inherit=True)
    exec(code, namespace, namespace)
    validator = namespace.get("persistent_evidence_errors")
    if not callable(validator):
        raise RuntimeError("validator snapshot lacks persistent_evidence_errors")
    report = json.loads(
        detail_bytes.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError("non-finite evidence JSON value: " + value)
        ),
    )
    if not isinstance(report, dict):
        raise ValueError("evidence root is not an object")
    errors = validator(report, eligibility=eligibility)
    if not isinstance(errors, list) or any(
        not isinstance(error, str) or not error for error in errors
    ):
        raise RuntimeError("validator snapshot returned an invalid error list")
except BaseException as exc:
    errors = [f"validator bootstrap failed: {type(exc).__name__}: {exc}"]

response = {
    "ok": not errors,
    "eligibility": payload.get("eligibility") if isinstance(payload, dict) else None,
    "errors": errors,
}
sys.stdout.write(json.dumps(response, ensure_ascii=False, sort_keys=True) + "\n")
raise SystemExit(0 if not errors else 1)
"""


def _rehearsal_detail_semantic_errors(detail: Mapping[str, Any]) -> list[str]:
    from .rehearsal import rehearsal_report_errors

    return rehearsal_report_errors(detail)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a stable summary for volatile Kafa evidence.")
    subparsers = parser.add_subparsers(dest="kind", required=True)
    for name in ("native", "rehearsal"):
        command = subparsers.add_parser(name)
        command.add_argument("--detail", required=True)
        command.add_argument("--out", required=True)
        command.add_argument("--change-scope-report")
        command.add_argument("--expected-decision-sha256")
        command.add_argument(
            "--legacy-historical",
            action="store_true",
            help="Import decision-less historical detail as blocking/unknown only.",
        )
        command.add_argument(
            "--eligibility",
            choices=sorted(EVIDENCE_ELIGIBILITY),
            default="historical-integrity",
        )
        command.add_argument("--validator-repo")
        command.add_argument("--retention-class", required=True, choices=sorted(RETENTION_CLASSES))
        command.add_argument("--retention-locator", default="")
        command.add_argument("--retention-days", type=int)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--summary", required=True)
    verify.add_argument("--detail", required=True)
    verify.add_argument("--change-scope-report")
    verify.add_argument("--expected-decision-sha256")
    verify.add_argument(
        "--eligibility",
        choices=sorted(EVIDENCE_ELIGIBILITY),
        default="historical-integrity",
    )
    verify.add_argument("--validator-repo")
    args = parser.parse_args(argv)
    try:
        if args.kind == "verify":
            summary_path = Path(args.summary).expanduser().resolve()
            summary = _load_detail(read_regular_detail(summary_path))
            errors = summary_detail_errors(
                summary,
                Path(args.detail).expanduser().resolve(),
                eligibility=args.eligibility,
                validator_repo=(
                    Path(args.validator_repo).expanduser().resolve()
                    if args.validator_repo
                    else None
                ),
                decision_path=(
                    Path(args.change_scope_report).expanduser().resolve()
                    if args.change_scope_report
                    else None
                ),
                expected_decision_sha256=args.expected_decision_sha256,
            )
            if errors:
                raise EvidenceSummaryError("; ".join(errors))
            print(f"OK: evidence detail matches {summary_path}")
            return 0
        detail_path = Path(args.detail).expanduser().resolve()
        detail_bytes = read_regular_detail(detail_path)
        decision_bytes = (
            read_regular_detail(Path(args.change_scope_report).expanduser().resolve())
            if args.change_scope_report
            else None
        )
        if decision_bytes is None:
            if not args.legacy_historical:
                raise EvidenceSummaryError(
                    "new evidence requires --change-scope-report; "
                    "decision-less import requires --legacy-historical"
                )
            if args.eligibility != "historical-integrity":
                raise EvidenceSummaryError(
                    "legacy historical import cannot request current eligibility"
                )
            requirement = "blocking"
            scopes = ["unknown"]
            allow_legacy_historical_scope = True
        else:
            if args.legacy_historical:
                raise EvidenceSummaryError(
                    "classifier-bound evidence derives requirement and scopes from its decision"
                )
            requirement = None
            scopes = []
            allow_legacy_historical_scope = False
        retention = {
            "class": args.retention_class,
            "locator": args.retention_locator or None,
            "days": args.retention_days,
        }
        summary = (
            summarize_native_detail(
                detail_bytes,
                decision_bytes=decision_bytes,
                expected_decision_sha256=args.expected_decision_sha256,
                requirement=requirement,
                change_scopes=scopes,
                eligibility=args.eligibility,
                validator_repo=(
                    Path(args.validator_repo).expanduser().resolve()
                    if args.validator_repo
                    else None
                ),
                allow_legacy_historical_scope=allow_legacy_historical_scope,
                retention=retention,
            )
            if args.kind == "native"
            else summarize_rehearsal_detail(
                detail_bytes,
                decision_bytes=decision_bytes,
                expected_decision_sha256=args.expected_decision_sha256,
                requirement=requirement,
                change_scopes=scopes,
                eligibility=args.eligibility,
                validator_repo=(
                    Path(args.validator_repo).expanduser().resolve()
                    if args.validator_repo
                    else None
                ),
                allow_legacy_historical_scope=allow_legacy_historical_scope,
                retention=retention,
            )
        )
        write_summary(Path(args.out).expanduser().resolve(), summary)
    except (OSError, EvidenceSummaryError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"OK: evidence summary written to {Path(args.out).expanduser().resolve()}")
    return 0


def read_regular_detail(path: Path) -> bytes:
    try:
        return read_regular_file(path).payload
    except ArtifactSubjectError as exc:
        raise EvidenceSummaryError(
            f"evidence detail is unavailable or changed while reading: {path}"
        ) from exc


def write_summary(path: Path, summary: Mapping[str, Any]) -> None:
    payload = summary_json_bytes(summary)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise EvidenceSummaryError(f"summary output is not a regular file: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_detail(detail_bytes: bytes) -> dict[str, Any]:
    if not isinstance(detail_bytes, bytes) or not detail_bytes:
        raise EvidenceSummaryError("evidence detail bytes are empty")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise EvidenceSummaryError(f"evidence detail contains duplicate key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            detail_bytes.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                EvidenceSummaryError(f"evidence detail contains non-finite number: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceSummaryError(f"evidence detail is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceSummaryError("evidence detail root is not an object")
    return value


def _require_str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvidenceSummaryError(f"{label} is missing")
    return value


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise EvidenceSummaryError(f"{label} is not a boolean")
    return value


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceSummaryError(f"invalid evidence timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise EvidenceSummaryError(f"evidence timestamp lacks timezone: {value}")
    return parsed


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(SHA256_PATTERN.fullmatch(value))
        and value != "0" * 64
    )


def _valid_oid(value: object) -> bool:
    return isinstance(value, str) and bool(OID_PATTERN.fullmatch(value)) and value != "0" * len(value)


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _safe_repository_locator(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


if __name__ == "__main__":
    raise SystemExit(main())
