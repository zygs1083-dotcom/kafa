"""Connector-origin trust token helpers.

The runtime can only treat connector-origin anchors as high trust when a host
outside the model process provides a shared HMAC key.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


CONNECTOR_KEY_ENV = "HARNESS_CONNECTOR_KEY"
CONNECTOR_KEY_PATH_FILE = Path(".ai-team/control/connector-key-path.txt")


class ConnectorTrustError(ValueError):
    """Raised when a connector-origin token is malformed or mismatched."""


@dataclass(frozen=True)
class ConnectorKey:
    value: str
    source: str


def _resolve_configured_key_path(root: Path) -> Path | None:
    config = root / CONNECTOR_KEY_PATH_FILE
    if not config.exists():
        return None
    configured = config.read_text(encoding="utf-8").strip()
    if not configured:
        return None
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = root / path
    return path


def configured_key_path(root: Path) -> Path | None:
    return _resolve_configured_key_path(root)


def load_connector_key(root: Path) -> ConnectorKey | None:
    env_key = os.environ.get(CONNECTOR_KEY_ENV, "").strip()
    if env_key:
        return ConnectorKey(env_key, CONNECTOR_KEY_ENV)
    path = _resolve_configured_key_path(root)
    if path is None or not path.exists() or not path.is_file():
        return None
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        return None
    return ConnectorKey(key, str(path))


def connector_hmac(key: str, payload: str) -> str:
    return hmac.new(key.encode("utf-8"), payload.encode("utf-8"), sha256).hexdigest()


def ci_payload(provider: str, run_id: str, commit_sha: str, conclusion: str) -> str:
    return f"ci:{provider}:{run_id}:{commit_sha}:{conclusion}"


def external_session_payload(session_id: str, verifier: str, commit_sha: str, conclusion: str) -> str:
    return f"external-session:{session_id}:{verifier}:{commit_sha}:{conclusion}"


def agent_session_payload(session_id: str, agent_id: str, role: str, context_id: str) -> str:
    return f"agent-session:{session_id}:{agent_id}:{role}:{context_id}"


def prepare_connector_record(root: Path, origin: str, provided_token: str, payload: str) -> tuple[str, str, str, str]:
    if origin != "connector":
        return origin, provided_token if origin == "manual" else "", "manual", ""
    if not provided_token:
        raise ConnectorTrustError("connector origin requires an externally issued verification_token")
    key = load_connector_key(root)
    if key is None:
        raise ConnectorTrustError("connector verifier key unavailable")
    expected = connector_hmac(key.value, payload)
    if not hmac.compare_digest(provided_token, expected):
        raise ConnectorTrustError("verification_token does not match connector HMAC")
    return "connector", provided_token, "hmac-valid", f"verified external receipt with {key.source}"


def verify_connector_record(root: Path, token: str, payload: str) -> tuple[bool, str]:
    key = load_connector_key(root)
    if key is None:
        return False, "connector key unavailable"
    if not token:
        return False, "connector verification_token is empty"
    expected = connector_hmac(key.value, payload)
    if not hmac.compare_digest(token, expected):
        return False, "connector HMAC mismatch"
    return True, "connector HMAC verified"
