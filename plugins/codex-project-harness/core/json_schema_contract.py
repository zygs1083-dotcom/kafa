"""Closed, dependency-free JSON schema subset used by shipped Kafa schemas."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any


SUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "title",
        "description",
        "readOnly",
        "type",
        "required",
        "properties",
        "items",
        "enum",
        "const",
        "minimum",
        "minLength",
        "pattern",
        "format",
        "additionalProperties",
        "allOf",
        "oneOf",
        "if",
        "then",
        "else",
    }
)
SUPPORTED_TYPES = frozenset(
    {"null", "string", "integer", "array", "object", "boolean"}
)
SUPPORTED_FORMATS = frozenset({"date-time"})


def json_type_matches(value: Any, expected: str | list[str]) -> bool:
    options = expected if isinstance(expected, list) else [expected]
    for option in options:
        if option == "null" and value is None:
            return True
        if option == "string" and isinstance(value, str):
            return True
        if option == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if option == "array" and isinstance(value, list):
            return True
        if option == "object" and isinstance(value, dict):
            return True
        if option == "boolean" and isinstance(value, bool):
            return True
    return False


def schema_definition_issues(
    schema: object,
    *,
    path: str = "$",
) -> list[str]:
    """Validate that one schema uses only the implemented closed subset."""

    if not isinstance(schema, dict):
        return [f"schema definition failed: {path} must be an object"]
    issues: list[str] = []
    for keyword in schema:
        if keyword not in SUPPORTED_SCHEMA_KEYWORDS:
            issues.append(
                f"unsupported schema keyword: {path}.{keyword}"
            )

    for keyword in ("$schema", "$id"):
        if keyword in schema and (
            not isinstance(schema[keyword], str) or not schema[keyword].strip()
        ):
            issues.append(
                f"schema definition failed: {path}.{keyword} must be a non-empty string"
            )
    for keyword in ("title", "description"):
        if keyword in schema and not isinstance(schema[keyword], str):
            issues.append(
                f"schema definition failed: {path}.{keyword} must be a string"
            )
    if "readOnly" in schema and not isinstance(schema["readOnly"], bool):
        issues.append(
            f"schema definition failed: {path}.readOnly must be boolean"
        )

    expected_type = schema.get("type")
    if expected_type is not None:
        types = expected_type if isinstance(expected_type, list) else [expected_type]
        if not types or any(item not in SUPPORTED_TYPES for item in types):
            issues.append(f"schema definition failed: {path}.type is invalid")
        elif len(types) != len(set(types)):
            issues.append(
                f"schema definition failed: {path}.type entries must be unique"
            )
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list)
        or any(not isinstance(item, str) or not item for item in required)
    ):
        issues.append(f"schema definition failed: {path}.required must be string array")
    elif isinstance(required, list) and len(required) != len(set(required)):
        issues.append(
            f"schema definition failed: {path}.required entries must be unique"
        )
    enum = schema.get("enum")
    if "enum" in schema:
        if not isinstance(enum, list) or not enum:
            issues.append(
                f"schema definition failed: {path}.enum must be a non-empty array"
            )
        else:
            try:
                identities = [
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    for item in enum
                ]
            except (TypeError, ValueError):
                issues.append(
                    f"schema definition failed: {path}.enum values must be JSON values"
                )
            else:
                if len(identities) != len(set(identities)):
                    issues.append(
                        f"schema definition failed: {path}.enum entries must be unique"
                    )
    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        issues.append(
            f"schema definition failed: {path}.additionalProperties must be boolean"
        )
    minimum = schema.get("minimum")
    if minimum is not None and (
        not isinstance(minimum, (int, float)) or isinstance(minimum, bool)
    ):
        issues.append(f"schema definition failed: {path}.minimum must be numeric")
    min_length = schema.get("minLength")
    if min_length is not None and (
        not isinstance(min_length, int)
        or isinstance(min_length, bool)
        or min_length < 0
    ):
        issues.append(
            f"schema definition failed: {path}.minLength must be a non-negative integer"
        )
    pattern = schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            issues.append(f"schema definition failed: {path}.pattern must be a string")
        else:
            try:
                re.compile(pattern)
            except re.error as exc:
                issues.append(
                    f"schema definition failed: {path}.pattern is invalid: {exc}"
                )
    schema_format = schema.get("format")
    if schema_format is not None and schema_format not in SUPPORTED_FORMATS:
        issues.append(
            f"schema definition failed: {path}.format is unsupported: {schema_format}"
        )

    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            issues.append(f"schema definition failed: {path}.properties must be an object")
        else:
            for name, definition in properties.items():
                if not isinstance(name, str) or not name:
                    issues.append(
                        f"schema definition failed: {path}.properties has invalid name"
                    )
                    continue
                issues.extend(
                    schema_definition_issues(
                        definition,
                        path=f"{path}.properties.{name}",
                    )
                )
    if "items" in schema:
        issues.extend(
            schema_definition_issues(
                schema["items"],
                path=f"{path}.items",
            )
        )
    for keyword in ("allOf", "oneOf"):
        if keyword not in schema:
            continue
        branches = schema[keyword]
        if not isinstance(branches, list) or not branches:
            issues.append(
                f"schema definition failed: {path}.{keyword} must be a non-empty array"
            )
            continue
        for index, branch in enumerate(branches):
            issues.extend(
                schema_definition_issues(
                    branch,
                    path=f"{path}.{keyword}[{index}]",
                )
            )
    for keyword in ("if", "then", "else"):
        if keyword in schema:
            issues.extend(
                schema_definition_issues(
                    schema[keyword],
                    path=f"{path}.{keyword}",
                )
            )
    if ("then" in schema or "else" in schema) and "if" not in schema:
        issues.append(
            f"schema definition failed: {path}.then/else requires if"
        )
    return issues


def validate_instance(
    label: str,
    value: Any,
    schema: Mapping[str, Any],
) -> list[str]:
    """Validate one value against the complete shipped Kafa schema subset."""

    definition_issues = schema_definition_issues(schema)
    if definition_issues:
        return [
            "schema contract failed: invalid schema definition: " + issue
            for issue in definition_issues
        ]
    return _validate_instance(label, value, schema)


def _validate_instance(
    label: str,
    value: Any,
    schema: Mapping[str, Any],
) -> list[str]:
    """Validate an instance after the complete root schema was checked."""

    issues: list[str] = []
    expected_type = schema.get("type")
    if expected_type is not None and not json_type_matches(value, expected_type):
        return [
            f"schema contract failed: {label} expected {expected_type}, "
            f"got {type(value).__name__}"
        ]

    if "enum" in schema and value not in schema["enum"]:
        issues.append(
            f"schema contract failed: {label}={value} not in {schema['enum']}"
        )
    if "const" in schema and value != schema["const"]:
        issues.append(
            f"schema contract failed: {label} violates const={schema['const']!r}"
        )
    if "minimum" in schema and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema["minimum"]:
            issues.append(
                f"schema contract failed: {label} violates minimum={schema['minimum']}"
            )
    if "minLength" in schema and isinstance(value, str):
        if len(value) < schema["minLength"]:
            issues.append(
                f"schema contract failed: {label} violates minLength={schema['minLength']}"
            )
    if "pattern" in schema and isinstance(value, str):
        try:
            matched = re.search(schema["pattern"], value)
        except re.error as exc:  # guarded by structure validation; fail closed at runtime too
            issues.append(
                f"schema contract failed: {label} has invalid pattern: {exc}"
            )
        else:
            if matched is None:
                issues.append(
                    f"schema contract failed: {label} violates pattern={schema['pattern']!r}"
                )
    if schema.get("format") == "date-time" and isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is None or parsed.tzinfo is None:
            issues.append(
                f"schema contract failed: {label} violates format='date-time'"
            )

    for branch in schema.get("allOf", []):
        issues.extend(_validate_instance(label, value, branch))

    one_of = schema.get("oneOf", [])
    if one_of:
        branch_results = [
            _validate_instance(label, value, branch)
            for branch in one_of
        ]
        matching = [
            index for index, branch_issues in enumerate(branch_results)
            if not branch_issues
        ]
        if len(matching) != 1:
            issues.append(
                f"schema contract failed: {label} oneOf matched "
                f"{len(matching)} branches, expected exactly one"
            )
            if not matching:
                for index, branch_issues in enumerate(branch_results):
                    issues.extend(
                        f"schema contract failed: {label} oneOf[{index}]: {issue}"
                        for issue in branch_issues
                    )

    if_schema = schema.get("if")
    if isinstance(if_schema, Mapping):
        condition_matches = not _validate_instance(label, value, if_schema)
        selected = schema.get("then" if condition_matches else "else")
        if isinstance(selected, Mapping):
            issues.extend(_validate_instance(label, value, selected))

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for field in required:
            if field not in value:
                issues.append(
                    f"schema contract failed: {label}.{field} is required"
                )
        if schema.get("additionalProperties") is False:
            for field in value:
                if field not in properties:
                    issues.append(
                        f"schema contract failed: {label}.{field} is not declared"
                    )
        for field, definition in properties.items():
            if field in value:
                issues.extend(
                    _validate_instance(
                        f"{label}.{field}",
                        value[field],
                        definition,
                    )
                )
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            issues.extend(
                _validate_instance(
                    f"{label}[{index}]",
                    item,
                    schema["items"],
                )
            )
    return issues
