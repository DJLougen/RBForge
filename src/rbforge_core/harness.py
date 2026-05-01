"""Starter harness combining built-ins with forged runtime tools."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from rbforge_core.debugger import debugger_signal_report
from rbforge_core.rbmem import RbmemStore


class ToolHarness:
    def __init__(
        self,
        memory_path: str | Path = "memory.rbmem",
        rbmem_cli: str | None = None,
    ) -> None:
        self.store = RbmemStore(memory_path, rbmem_cli=rbmem_cli)

    def ripgrep(self, pattern: str, root: str | Path = ".") -> str:
        cmd = ["rg", "-n", pattern, str(root)]
        completed = subprocess.run(cmd, text=True, capture_output=True, check=False)
        if completed.returncode not in {0, 1}:
            raise RuntimeError(completed.stderr.strip())
        return completed.stdout

    def debugger_summary(self, text: str) -> dict[str, Any]:
        return debugger_signal_report(text)

    def call_forged(self, name: str, arguments: dict[str, Any]) -> Any:
        record = self._load_tool_record(name)
        if record.get("language") != "python":
            raise RuntimeError(
                f"only python forged tools are callable in this starter harness: {name}"
            )
        implementation = record["implementation"]
        function_name = (
            name if re.search(rf"def\s+{re.escape(name)}\s*\(", implementation) else "run"
        )
        with tempfile.TemporaryDirectory(prefix="RBForge-call-") as tmp:
            root = Path(tmp)
            (root / "tool_impl.py").write_text(implementation, encoding="utf-8")
            runner = root / "runner.py"
            runner.write_text(
                "import importlib, json, sys\n"
                "args = json.loads(sys.argv[1])\n"
                "mod = importlib.import_module('tool_impl')\n"
                f"result = getattr(mod, {function_name!r})(**args)\n"
                "print(json.dumps(result))\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                ["python", str(runner), json.dumps(arguments)],
                cwd=root,
                text=True,
                capture_output=True,
                timeout=8,
                check=False,
            )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip())
        return json.loads(completed.stdout)

    def _load_tool_record(self, name: str) -> dict[str, Any]:
        return self.store.load_tool_record(name)
