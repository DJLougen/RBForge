"""Validation helpers for model-proposed tool specs."""

from __future__ import annotations

import ast
import re
from typing import Any

from rbforge_core.models import ToolSpec

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_ALLOWED_SCHEMA_TYPES = {"object", "string", "number", "integer", "boolean", "array", "null"}


class ToolSpecError(ValueError):
    """Raised when a forged tool proposal is malformed."""


def validate_tool_spec(spec: ToolSpec) -> None:
    if not _NAME_RE.match(spec.name):
        raise ToolSpecError("tool name must be snake_case, start with a letter, and be 3-64 chars")
    if not spec.description.strip() or len(spec.description.strip()) < 12:
        raise ToolSpecError("description must be a clear one-sentence purpose")
    if spec.language not in {"python", "bash", "rust"}:
        raise ToolSpecError(f"unsupported language: {spec.language}")
    _validate_schema_shape(spec.schema)
    if spec.language == "python":
        _validate_python_source(spec)


def _validate_schema_shape(schema: dict[str, Any]) -> None:
    if not isinstance(schema, dict):
        raise ToolSpecError("schema must be a JSON object")
    if schema.get("type") != "object":
        raise ToolSpecError("tool argument schema must be an object schema")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ToolSpecError("schema.properties must be an object")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise ToolSpecError("schema.required must be a list of property names")
    for key, value in properties.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            raise ToolSpecError("schema.properties entries must be JSON schema objects")
        declared_type = value.get("type")
        if isinstance(declared_type, str) and declared_type not in _ALLOWED_SCHEMA_TYPES:
            raise ToolSpecError(f"unsupported JSON schema type for {key}: {declared_type}")


def _validate_python_source(spec: ToolSpec) -> None:
    try:
        tree = ast.parse(spec.implementation)
    except SyntaxError as exc:
        raise ToolSpecError(f"python implementation has syntax error: {exc}") from exc

    function_names = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    if "run" not in function_names and spec.name not in function_names:
        raise ToolSpecError(
            "python tools must define run(...) or a function matching the tool name"
        )

    forbidden_imports = {"subprocess", "socket", "requests", "urllib", "httpx", "shutil"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in forbidden_imports:
                    raise ToolSpecError(f"forbidden import in forged tool: {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            if root in forbidden_imports:
                raise ToolSpecError(f"forbidden import in forged tool: {node.module}")
