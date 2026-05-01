"""RBForge meta-tool.

This module is the agent-facing runtime tool creation layer. It validates a
model-proposed tool, persists it into Rust-Brain RBMEM under
``tools.custom.{name}``, sandbox-runs generated tests, registers graph edges,
and returns clean JSON that can be emitted from Hermes-style tool calls inside
``<think>...</think>`` reasoning traces.

Example:
    result = forge_tool(
        name="count_tracebacks",
        description="Count Python tracebacks in a log.",
        schema={
            "type": "object",
            "properties": {"log": {"type": "string", "default": "Traceback"}},
            "required": ["log"],
        },
        implementation=(
            "def run(log: str) -> dict:\\n"
            "    return {'count': log.count('Traceback')}\\n"
        ),
        category="debugger",
        dependencies=["tools.builtin.ripgrep"],
    )
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator, ValidationError

ToolLanguage = Literal["python", "bash", "rust"]

FORGE_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "description", "schema", "implementation", "category"],
    "properties": {
        "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]{2,63}$"},
        "description": {"type": "string", "minLength": 12},
        "schema": {"type": "object"},
        "implementation": {"type": "string", "minLength": 10},
        "category": {"type": "string", "minLength": 2},
        "dependencies": {"type": "array", "items": {"type": "string"}},
        "language": {"type": "string", "enum": ["python", "bash", "rust"]},
        "expected_args": {"type": "object"},
        "expected_output_keys": {"type": "array", "items": {"type": "string"}},
        "review_required": {"type": "boolean"},
        "forged_by": {"type": "string"},
    },
}

HIGH_IMPACT_CATEGORIES = {"filesystem", "memory", "shell", "web_bubble"}
NETWORK_CATEGORIES = {"web_bubble", "social_monitor", "web_research"}
SHELL_CATEGORIES = {"shell"}
SAFE_PYTHON_IMPORTS = {
    "collections",
    "dataclasses",
    "datetime",
    "decimal",
    "functools",
    "hashlib",
    "heapq",
    "itertools",
    "json",
    "math",
    "re",
    "statistics",
    "string",
    "typing",
}
NETWORK_PYTHON_IMPORTS = {
    "http",
    "httpx",
    "requests",
    "urllib",
}
SHELL_PYTHON_IMPORTS = {
    "shlex",
    "subprocess",
}
FORBIDDEN_CALLS = {"eval", "exec", "compile", "open", "__import__"}
FORBIDDEN_IMPORTS = {
    "asyncio",
    "ctypes",
    "importlib",
    "multiprocessing",
    "os",
    "pathlib",
    "shutil",
    "signal",
    "socket",
    "sys",
    "threading",
}


@dataclass(frozen=True)
class ToolSpec:
    """Validated model proposal for a forged tool."""

    name: str
    description: str
    schema: dict[str, Any]
    implementation: str
    category: str
    dependencies: list[str] = field(default_factory=list)
    language: ToolLanguage = "python"
    expected_args: dict[str, Any] | None = None
    expected_output_keys: list[str] = field(default_factory=list)
    review_required: bool = False
    forged_by: str = "rbforge-agent"
    version: str = "0.1.0"

    @property
    def section_path(self) -> str:
        return f"tools.custom.{self.name}"


@dataclass(frozen=True)
class SandboxReport:
    """Sandbox validation outcome suitable for DDM traces."""

    ok: bool
    backend: str
    returncode: int
    stdout_tail: str
    stderr_tail: str
    generated_test: str
    static_warnings: list[str]


def forge_tool(
    *,
    name: str,
    description: str,
    schema: dict[str, Any],
    implementation: str,
    category: str,
    dependencies: list[str] | None = None,
    language: ToolLanguage = "python",
    expected_args: dict[str, Any] | None = None,
    expected_output_keys: list[str] | None = None,
    memory_path: str | Path = "memory.rbmem",
    rbmem_cli: str | None = None,
    trace_path: str | Path | None = "data/traces/RBForge.jsonl",
    review_required: bool = False,
    forged_by: str = "rbforge-agent",
    timeout_seconds: int = 8,
) -> dict[str, Any]:
    """Create, validate, persist, and register a runtime tool.

    Returns:
        A JSON-serializable dict. Agents can directly use this as the
        observation for a Hermes-compatible ``forge_tool`` tool call.
    """
    spec = ToolSpec(
        name=name,
        description=description,
        schema=schema,
        implementation=implementation,
        category=category,
        dependencies=dependencies or [],
        language=language,
        expected_args=expected_args,
        expected_output_keys=expected_output_keys or [],
        review_required=review_required,
        forged_by=forged_by,
    )
    logger = TraceLogger(trace_path)
    logger.record("forge_tool.requested", {"name": name, "category": category})

    try:
        validate_spec(spec)
    except (RBForgeError, ValidationError) as exc:
        logger.record("forge_tool.rejected", {"name": name, "error": str(exc)})
        return _failure("validation_failed", name, str(exc))

    store = RbmemStore(memory_path, rbmem_cli=rbmem_cli)
    store.ensure()
    store.update_section(spec.section_path, _tool_record(spec, status="candidate"))
    store.apply_graph(spec.section_path, "tool", _relations(spec, registered=False))
    logger.record("forge_tool.candidate_persisted", {"section": spec.section_path})

    sandbox = sandbox_validate(spec, timeout_seconds=timeout_seconds)
    logger.record("forge_tool.sandbox_finished", asdict(sandbox))
    review = spec.review_required or spec.category in HIGH_IMPACT_CATEGORIES
    registry_size = len(store.read_registry())

    if sandbox.ok and not review:
        record = _tool_record(spec, status="validated", sandbox=sandbox)
        store.update_section(spec.section_path, record)
        store.apply_graph(spec.section_path, "tool", _relations(spec, registered=True))
        registry_size = store.register_tool(record)
        logger.record(
            "forge_tool.registered",
            {"name": spec.name, "section": spec.section_path, "registry_size": registry_size},
        )
        status = "registered"
    elif sandbox.ok and review:
        store.update_section(
            f"tools.review_queue.{spec.name}",
            {
                "tool": spec.section_path,
                "category": spec.category,
                "reason": "high-impact tool requires human review before registry activation",
                "queued_at": utc_now(),
            },
        )
        store.apply_graph(
            f"tools.review_queue.{spec.name}",
            "review_item",
            [{"to": spec.section_path, "type": "reviews"}],
        )
        logger.record("forge_tool.review_queued", {"name": spec.name})
        status = "review_queued"
    else:
        failed = _tool_record(spec, status="sandbox_failed", sandbox=sandbox)
        store.update_section(spec.section_path, failed)
        store.apply_graph(spec.section_path, "tool", _relations(spec, registered=False))
        logger.record("forge_tool.failed", {"name": spec.name, "stderr": sandbox.stderr_tail})
        status = "sandbox_failed"

    store.validate()
    context = store.context_preview(spec.name)
    return {
        "ok": sandbox.ok,
        "status": status,
        "name": spec.name,
        "section_path": spec.section_path,
        "rbmem_path": str(Path(memory_path)),
        "registry_size": registry_size,
        "review_required": review,
        "sandbox": asdict(sandbox),
        "graph_edges": _relations(spec, registered=status == "registered"),
        "rbmem_diagnostics": store.doctor(),
        "hermes_next_step": {
            "call": spec.name if status == "registered" else None,
            "arguments_schema": spec.schema,
        },
        "rbmem_context_preview": context,
    }


def run_forged_tool(
    *,
    name: str,
    arguments: dict[str, Any],
    memory_path: str | Path = "memory.rbmem",
    rbmem_cli: str | None = None,
    trace_path: str | Path | None = "data/traces/RBForge.jsonl",
    timeout_seconds: int = 8,
) -> dict[str, Any]:
    """Run a registered forged tool and update RBMEM usage metrics.

    Example:
        run_forged_tool(name="count_tracebacks", arguments={"log": "Traceback"})
    """
    logger = TraceLogger(trace_path)
    store = RbmemStore(memory_path, rbmem_cli=rbmem_cli)
    record = store.load_tool_record(name)
    try:
        Draft202012Validator(record["schema"]).validate(arguments)
        output = execute_python_tool(
            record["implementation"],
            name=name,
            arguments=arguments,
            timeout_seconds=timeout_seconds,
        )
        ok = output["ok"]
        result = output.get("result")
        error = output.get("error", "")
    except Exception as exc:  # noqa: BLE001 - returned as clean tool JSON
        ok = False
        result = None
        error = str(exc)

    updated = _update_metrics(record, success=ok)
    store.update_section(f"tools.custom.{name}", updated)
    store.apply_graph(
        f"tools.custom.{name}",
        "tool",
        _relations_from_record(updated, registered=updated.get("status") == "validated"),
    )
    store.register_tool(updated)
    logger.record(
        "forge_tool.used",
        {"name": name, "ok": ok, "usage_count": updated["metrics"]["usage_count"]},
    )
    return {
        "ok": ok,
        "name": name,
        "section_path": f"tools.custom.{name}",
        "result": result,
        "error": error,
        "metrics": updated["metrics"],
    }


class RBForgeError(ValueError):
    """Raised for malformed or unsafe forged tool proposals."""


class TraceLogger:
    """Append-only JSONL trace logger for DDM and later SFT/RL filtering."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, payload: dict[str, Any]) -> None:
        if not self.path:
            return
        row = {"ts": utc_now(), "event": event, "payload": payload}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


class RbmemStore:
    """Small RBMEM CLI adapter that keeps Rust-Brain as the source of truth."""

    def __init__(self, memory_path: str | Path, rbmem_cli: str | None = None) -> None:
        self.memory_path = Path(memory_path)
        self.rbmem_cli = rbmem_cli or find_or_build_rbmem()

    def ensure(self) -> None:
        if self.memory_path.exists():
            return
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                self.rbmem_cli,
                "create",
                str(self.memory_path),
                "--created-by",
                "rbforge",
                "--purpose",
                "RBForge-runtime-tool-memory",
            ]
        )

    def update_section(self, section: str, content: dict[str, Any]) -> None:
        self.ensure()
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            json.dump(content, handle, indent=2, sort_keys=True)
            content_file = handle.name
        try:
            self._run(
                [
                    self.rbmem_cli,
                    "update",
                    str(self.memory_path),
                    "--section",
                    section,
                    "--type",
                    "json",
                    "--content-file",
                    content_file,
                ]
            )
        finally:
            Path(content_file).unlink(missing_ok=True)
        self.validate()

    def register_tool(self, record: dict[str, Any]) -> int:
        registry = self.read_registry()
        registry = [item for item in registry if item["name"] != record["name"]]
        registry.append(
            {
                "name": record["name"],
                "section": f"tools.custom.{record['name']}",
                "category": record["category"],
                "version": record["version"],
                "dependencies": record["dependencies"],
                "registered_at": record["registered_at"],
                "usage_count": record["metrics"]["usage_count"],
                "success_rate": record["metrics"]["success_rate"],
                "status": record["status"],
            }
        )
        registry.sort(key=lambda item: item["name"])
        self.update_section(
            "tools.registry",
            {
                "schema": "rbforge.tool_registry.v1",
                "updated_by": "RBForge",
                "tools": registry,
            },
        )
        self.apply_graph(
            "tools.registry",
            "tool_registry",
            [{"to": item["section"], "type": "indexes"} for item in registry],
        )
        return len(registry)

    def read_registry(self) -> list[dict[str, Any]]:
        try:
            payload = self.context("tools registry", resolve=True, minified=False, graph_depth=1)
        except Exception:  # noqa: BLE001 - corrupted/empty registry should self-heal
            return []
        for section in payload.get("sections", []):
            if section.get("path") == "tools.registry":
                content = json.loads(section.get("content") or "{}")
                tools = content.get("tools", [])
                return tools if isinstance(tools, list) else []
        return []

    def load_tool_record(self, name: str) -> dict[str, Any]:
        payload = self.context(f"tools custom {name}", resolve=True, minified=False, graph_depth=1)
        section_path = f"tools.custom.{name}"
        for section in payload.get("sections", []):
            if section.get("path") == section_path:
                return json.loads(section["content"])
        raise KeyError(f"forged tool not found: {section_path}")

    def read_minified(self) -> str:
        completed = self._run(
            [self.rbmem_cli, "read", str(self.memory_path), "--resolve", "--minified"],
            capture=True,
        )
        return completed.stdout

    def rbmem_version(self) -> str:
        completed = self._run([self.rbmem_cli, "--version"], capture=True)
        return completed.stdout.strip()

    def doctor(self) -> dict[str, Any]:
        self.ensure()
        completed = self._run(
            [
                self.rbmem_cli,
                "hermes",
                "doctor",
                str(self.memory_path),
                "--rbmem-cli",
                self.rbmem_cli,
                "--format",
                "json",
            ],
            capture=True,
        )
        return json.loads(completed.stdout)

    def context(
        self,
        query: str,
        *,
        resolve: bool = True,
        minified: bool = True,
        graph_depth: int = 1,
    ) -> dict[str, Any]:
        self.ensure()
        args = [
            self.rbmem_cli,
            "query",
            str(self.memory_path),
            query,
            "--graph-depth",
            str(graph_depth),
            "--format",
            "json",
        ]
        if resolve:
            args.append("--resolve")
        if minified:
            args.append("--minified")
        completed = self._run(args, capture=True)
        return json.loads(completed.stdout)

    def context_preview(self, query: str, *, limit: int = 1200) -> str:
        payload = self.context(query, resolve=True, minified=True, graph_depth=1)
        context = payload.get("context", "")
        return context[:limit] if isinstance(context, str) else ""

    def hermes_load(self, *, resolve: bool = True, minified: bool = True) -> dict[str, Any]:
        args = [self.rbmem_cli, "hermes", "load", str(self.memory_path)]
        if resolve:
            args.append("--resolve")
        if minified:
            args.append("--minified")
        completed = self._run(args, capture=True)
        return json.loads(completed.stdout)

    def hermes_save(self, payload: dict[str, Any]) -> None:
        self._run(
            [
                self.rbmem_cli,
                "hermes",
                "save",
                str(self.memory_path),
                "--json",
                json.dumps(payload, separators=(",", ":")),
            ]
        )
        self.validate()

    def apply_graph(
        self,
        section: str,
        node_type: str,
        relations: list[dict[str, str]],
    ) -> None:
        text = self.memory_path.read_text(encoding="utf-8")
        patched = patch_section_graph(text, section, node_type, relations)
        self.memory_path.write_text(patched, encoding="utf-8")
        self.validate()

    def validate(self) -> None:
        self._run([self.rbmem_cli, "validate", str(self.memory_path)])

    def _run(self, args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(args, text=True, capture_output=True, check=False)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"rbmem failed: {' '.join(args)}\n{detail}")
        return completed


def validate_spec(spec: ToolSpec) -> None:
    """Validate the proposal envelope, argument schema, source, and smoke args."""
    Draft202012Validator(FORGE_TOOL_INPUT_SCHEMA).validate(
        {
            "name": spec.name,
            "description": spec.description,
            "schema": spec.schema,
            "implementation": spec.implementation,
            "category": spec.category,
            "dependencies": spec.dependencies,
            "language": spec.language,
            "expected_args": spec.expected_args or {},
            "expected_output_keys": spec.expected_output_keys,
            "review_required": spec.review_required,
            "forged_by": spec.forged_by,
        }
    )
    Draft202012Validator.check_schema(spec.schema)
    if spec.schema.get("type") != "object":
        raise RBForgeError("tool schema must be an object schema")
    Draft202012Validator(spec.schema).validate(spec.expected_args or sample_args(spec.schema))
    if spec.language != "python":
        raise RBForgeError("only python tools are executable in this release")
    validate_python_source(spec)


def validate_python_source(spec: ToolSpec) -> None:
    """Reject dangerous imports/calls and require a callable entrypoint."""
    try:
        tree = ast.parse(spec.implementation)
    except SyntaxError as exc:
        raise RBForgeError(f"syntax error in implementation: {exc}") from exc
    functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    if "run" not in functions and spec.name not in functions:
        raise RBForgeError("python implementation must define run(...) or def {tool_name}(...)")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _validate_import(alias.name, spec.category)
        elif isinstance(node, ast.ImportFrom) and node.module:
            _validate_import(node.module, spec.category)
        elif isinstance(node, ast.Call):
            call = _call_name(node)
            if call in FORBIDDEN_CALLS:
                raise RBForgeError(f"forbidden call in implementation: {call}")


def sandbox_validate(spec: ToolSpec, *, timeout_seconds: int) -> SandboxReport:
    """Run generated tests in Docker, falling back to a restricted subprocess."""
    warnings = static_warnings(spec.implementation)
    generated_test = generated_unittest(spec)
    with tempfile.TemporaryDirectory(prefix="rbforge-") as tmp:
        root = Path(tmp)
        (root / "tool_impl.py").write_text(spec.implementation, encoding="utf-8")
        (root / "test_tool.py").write_text(generated_test, encoding="utf-8")
        if docker_ready(timeout_seconds):
            return run_docker(root, generated_test, warnings, timeout_seconds)
        return run_local_subprocess(root, generated_test, warnings, timeout_seconds)


def run_docker(
    root: Path,
    generated_test: str,
    warnings: list[str],
    timeout_seconds: int,
) -> SandboxReport:
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--cpus",
        "1",
        "--memory",
        "256m",
        "--pids-limit",
        "128",
        "-v",
        f"{root}:/work:ro",
        "-w",
        "/work",
        "python:3.11-slim",
        "python",
        "-m",
        "unittest",
        "-v",
        "test_tool.py",
    ]
    return run_command(cmd, "docker", generated_test, warnings, timeout_seconds)


def run_local_subprocess(
    root: Path,
    generated_test: str,
    warnings: list[str],
    timeout_seconds: int,
) -> SandboxReport:
    runner = root / "run_generated_tests.py"
    runner.write_text(
        "import sys\n"
        "import unittest\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        "suite = unittest.defaultTestLoader.loadTestsFromName('test_tool')\n"
        "result = unittest.TextTestRunner(verbosity=2).run(suite)\n"
        "raise SystemExit(0 if result.wasSuccessful() else 1)\n",
        encoding="utf-8",
    )
    env = {
        "PYTHONIOENCODING": "utf-8",
        "NO_PROXY": "*",
    }
    cmd = [sys.executable, "-I", str(runner)]
    return run_command(
        cmd,
        "restricted-subprocess",
        generated_test,
        warnings,
        timeout_seconds,
        cwd=root,
        env=env,
        preexec_fn=resource_limiter() if os.name == "posix" else None,
    )


def execute_python_tool(
    implementation: str,
    *,
    name: str,
    arguments: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    """Execute a persisted Python tool in the same restricted fallback harness."""
    with tempfile.TemporaryDirectory(prefix="rbforge-use-") as tmp:
        root = Path(tmp)
        (root / "tool_impl.py").write_text(implementation, encoding="utf-8")
        runner = root / "runner.py"
        function = name if re.search(rf"def\s+{re.escape(name)}\s*\(", implementation) else "run"
        runner.write_text(
            "import json\n"
            "import sys\n"
            f"sys.path.insert(0, {str(root)!r})\n"
            "import tool_impl\n"
            "args = json.loads(sys.argv[1])\n"
            f"result = getattr(tool_impl, {function!r})(**args)\n"
            "print(json.dumps(result, sort_keys=True))\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [sys.executable, "-I", str(runner), json.dumps(arguments)],
            cwd=root,
            env={"PYTHONIOENCODING": "utf-8", "NO_PROXY": "*"},
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    if completed.returncode != 0:
        return {"ok": False, "error": completed.stderr.strip()}
    return {"ok": True, "result": json.loads(completed.stdout)}


def run_command(
    cmd: list[str],
    backend: str,
    generated_test: str,
    warnings: list[str],
    timeout_seconds: int,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    preexec_fn: Any | None = None,
) -> SandboxReport:
    try:
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            preexec_fn=preexec_fn,
        )
    except subprocess.TimeoutExpired as exc:
        return SandboxReport(
            ok=False,
            backend=backend,
            returncode=124,
            stdout_tail=(exc.stdout or "")[-1200:],
            stderr_tail=((exc.stderr or "") + f"\ntimeout after {timeout_seconds}s")[-1200:],
            generated_test=generated_test,
            static_warnings=warnings,
        )
    return SandboxReport(
        ok=completed.returncode == 0 and not warnings,
        backend=backend,
        returncode=completed.returncode,
        stdout_tail=completed.stdout[-1200:],
        stderr_tail=completed.stderr[-1200:],
        generated_test=generated_test,
        static_warnings=warnings,
    )


def generated_unittest(spec: ToolSpec) -> str:
    args = spec.expected_args or sample_args(spec.schema)
    function = (
        f"tool_impl.{spec.name}" if f"def {spec.name}" in spec.implementation else "tool_impl.run"
    )
    return f'''"""Generated RBForge validation tests."""

import json
import unittest

import tool_impl


class RBForgeGeneratedTest(unittest.TestCase):
    def test_result_is_json_serializable(self):
        args = {json.dumps(args, indent=8, sort_keys=True)}
        result = {function}(**args)
        json.dumps(result, sort_keys=True)
        self.assertIsNotNone(result)

    def test_expected_keys(self):
        expected = {json.dumps(spec.expected_output_keys, sort_keys=True)}
        if not expected:
            self.skipTest("no expected output keys declared")
        args = {json.dumps(args, indent=8, sort_keys=True)}
        result = {function}(**args)
        self.assertIsInstance(result, dict)
        for key in expected:
            self.assertIn(key, result)


if __name__ == "__main__":
    unittest.main()
'''


def sample_args(schema: dict[str, Any]) -> dict[str, Any]:
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return {}
    args: dict[str, Any] = {}
    for key, spec in properties.items():
        if not isinstance(spec, dict):
            args[key] = None
            continue
        if "default" in spec:
            args[key] = spec["default"]
        elif spec.get("type") == "string":
            args[key] = "sample"
        elif spec.get("type") == "integer":
            args[key] = 1
        elif spec.get("type") == "number":
            args[key] = 1.0
        elif spec.get("type") == "boolean":
            args[key] = True
        elif spec.get("type") == "array":
            args[key] = []
        elif spec.get("type") == "object":
            args[key] = {}
        else:
            args[key] = None
    return args


def patch_section_graph(
    rbmem_text: str,
    section: str,
    node_type: str,
    relations: list[dict[str, str]],
) -> str:
    """Replace graph metadata inside one section and leave temporal data intact."""
    header = f"[SECTION: {section}]"
    start = rbmem_text.find(header)
    if start == -1:
        raise RuntimeError(f"section not found for graph patch: {section}")
    end = rbmem_text.find("[END SECTION]", start)
    if end == -1:
        raise RuntimeError(f"section missing end marker: {section}")
    end += len("[END SECTION]")
    block = rbmem_text[start:end]
    lines = block.splitlines(keepends=True)
    cleaned = remove_graph_blocks(lines)
    insert_at = 1
    for index, line in enumerate(cleaned):
        if line.startswith("type:"):
            insert_at = index + 1
            break
    graph_lines = render_graph_block(node_type, relations).splitlines(keepends=True)
    new_block = "".join(cleaned[:insert_at] + graph_lines + cleaned[insert_at:])
    return rbmem_text[:start] + new_block + rbmem_text[end:]


def remove_graph_blocks(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    index = 0
    while index < len(lines):
        if lines[index].strip() != "graph:":
            cleaned.append(lines[index])
            index += 1
            continue
        index += 1
        while index < len(lines):
            stripped = lines[index].strip()
            top_level = lines[index] and not lines[index].startswith((" ", "\t"))
            if top_level and stripped not in {"", "graph:"}:
                break
            index += 1
    return cleaned


def render_graph_block(node_type: str, relations: list[dict[str, str]]) -> str:
    lines = ["graph:\n", f"  node_type: {json.dumps(node_type)}\n", "  relations:\n"]
    seen: set[tuple[str, str]] = set()
    for relation in relations:
        key = (relation["to"], relation["type"])
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"    - to: {json.dumps(relation['to'])}\n")
        lines.append(f"      type: {json.dumps(relation['type'])}\n")
    return "".join(lines)


def docker_ready(timeout_seconds: int) -> bool:
    if not shutil.which("docker"):
        return False
    try:
        completed = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            text=True,
            capture_output=True,
            timeout=min(3, timeout_seconds),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    return completed.returncode == 0


def resource_limiter() -> Any | None:
    """Return POSIX resource limiter for fallback subprocesses when available."""

    def limit() -> None:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (4, 4))
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))

    return limit


def find_or_build_rbmem() -> str:
    env_path = os.environ.get("RBMEM_CLI")
    exe = "rbmem.exe" if os.name == "nt" else "rbmem"
    candidates = [
        env_path,
        shutil.which("rbmem"),
        shutil.which("rbmem.exe"),
        str(Path(tempfile.gettempdir()) / "rust-brain-inspect" / "target" / "release" / exe),
        str(Path.home() / "Rust-Brain" / "target" / "release" / exe),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    repo = Path(tempfile.gettempdir()) / "rust-brain-inspect"
    if not repo.exists():
        subprocess.run(
            ["git", "clone", "--depth", "1", "https://github.com/DJLougen/Rust-Brain", str(repo)],
            check=True,
        )
    subprocess.run(["cargo", "build", "--release"], cwd=repo, check=True)
    built = repo / "target" / "release" / ("rbmem.exe" if os.name == "nt" else "rbmem")
    if not built.exists():
        raise RuntimeError("failed to build rbmem CLI from Rust-Brain")
    return str(built)


def static_warnings(source: str) -> list[str]:
    warnings: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call = _call_name(node)
            if call in FORBIDDEN_CALLS:
                warnings.append(f"forbidden call: {call}")
    return warnings


def _validate_import(module: str, category: str) -> None:
    root = module.split(".", 1)[0]
    allowed = set(SAFE_PYTHON_IMPORTS)
    if category in NETWORK_CATEGORIES:
        allowed.update(NETWORK_PYTHON_IMPORTS)
    if category in SHELL_CATEGORIES:
        allowed.update(SHELL_PYTHON_IMPORTS)
    if root in FORBIDDEN_IMPORTS or root not in allowed:
        raise RBForgeError(f"forbidden import in implementation: {module}")


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _tool_record(
    spec: ToolSpec,
    *,
    status: str,
    sandbox: SandboxReport | None = None,
) -> dict[str, Any]:
    return {
        "record_schema": "rbforge.forged_tool.v1",
        "name": spec.name,
        "description": spec.description,
        "schema": spec.schema,
        "schema_version": "json-schema-2020-12",
        "implementation": spec.implementation,
        "language": spec.language,
        "category": spec.category,
        "dependencies": spec.dependencies,
        "version": spec.version,
        "status": status,
        "forged_by": spec.forged_by,
        "registered_at": utc_now(),
        "metrics": {
            "usage_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "success_rate": 0.0,
            "last_used_at": None,
        },
        "validation": asdict(sandbox) if sandbox else {},
        "timestamp_policy": "rbmem_cli_temporal_fields_are_authoritative",
    }


def _relations(spec: ToolSpec, *, registered: bool) -> list[dict[str, str]]:
    return _relations_from_record(
        {
            "name": spec.name,
            "category": spec.category,
            "dependencies": spec.dependencies,
        },
        registered=registered,
    )


def _relations_from_record(record: dict[str, Any], *, registered: bool) -> list[dict[str, str]]:
    relations = [
        {"to": "tools.registry", "type": "registered_in" if registered else "candidate_for"}
    ]
    relations.extend(
        {"to": dependency, "type": "depends_on"} for dependency in record["dependencies"]
    )
    relations.append({"to": f"tool_categories.{record['category']}", "type": "categorized_as"})
    return relations


def _update_metrics(record: dict[str, Any], *, success: bool) -> dict[str, Any]:
    updated = json.loads(json.dumps(record))
    metrics = updated.setdefault("metrics", {})
    metrics["usage_count"] = int(metrics.get("usage_count", 0)) + 1
    if success:
        metrics["success_count"] = int(metrics.get("success_count", 0)) + 1
    else:
        metrics["failure_count"] = int(metrics.get("failure_count", 0)) + 1
    total = metrics["success_count"] + metrics["failure_count"]
    metrics["success_rate"] = round(metrics["success_count"] / max(total, 1), 4)
    metrics["last_used_at"] = utc_now()
    return updated


def _failure(reason: str, name: str, error: str) -> dict[str, Any]:
    return {"ok": False, "status": reason, "name": name, "error": error}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
