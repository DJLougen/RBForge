"""RBForge health and usage diagnostics."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from rbforge_core import __version__
from rbforge_core.rbmem import RbmemStore


class DoctorStore(Protocol):
    memory_path: Path | str

    def rbmem_version(self) -> str: ...

    def doctor(self) -> dict[str, Any]: ...

    def read_registry(self) -> list[dict[str, Any]]: ...

    def context(
        self,
        query: str,
        *,
        resolve: bool = True,
        minified: bool = True,
        graph_depth: int = 1,
    ) -> dict[str, Any]: ...


def add_doctor_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "memory_path",
        nargs="?",
        default="memory.rbmem",
        help="RBMEM file to inspect.",
    )
    parser.add_argument(
        "--rbmem-cli",
        default=None,
        help="Path to rbmem or rbmem.exe. Defaults to RBMEM_CLI or PATH.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format.",
    )


def build_report(
    store: DoctorStore,
    *,
    rbforge_version: str = __version__,
) -> dict[str, Any]:
    """Collect RBForge, RBMEM, memory, registry, and forged-tool metrics."""
    memory_path = Path(store.memory_path)
    existed_before_check = memory_path.exists()
    rbmem_version = store.rbmem_version()
    doctor_payload = store.doctor()
    exists_after_check = memory_path.exists()
    registry = store.read_registry()
    tool_records = _read_tool_records(store, registry)
    validated_tools = sum(1 for record in tool_records if _is_validated(record))
    success_rates = list(_success_rates(tool_records))
    average_success_rate = (
        round(sum(success_rates) / len(success_rates), 4) if success_rates else None
    )
    category_metrics = _category_metrics(tool_records)
    debugger_records = [record for record in tool_records if _is_debugger_tool(record)]
    debugger_rates = list(_success_rates(debugger_records))
    forged_tools = len(tool_records)
    validation_rate = round(validated_tools / forged_tools, 4) if forged_tools else None

    return {
        "schema": "rbforge.doctor.v1",
        "rbforge_version": rbforge_version,
        "rbmem_cli_version": rbmem_version,
        "memory_path": str(memory_path),
        "memory_file": {
            "exists_before_check": existed_before_check,
            "exists": exists_after_check,
            "size_bytes": memory_path.stat().st_size if exists_after_check else 0,
            "health": _memory_health(doctor_payload),
        },
        "rbmem_doctor": doctor_payload,
        "registry_size": len(registry),
        "forged_tools": forged_tools,
        "validated_tools": validated_tools,
        "validation_rate": validation_rate,
        "average_success_rate": average_success_rate,
        "category_metrics": category_metrics,
        "debugger_tools": len(debugger_records),
        "debugger_validation_rate": _validation_rate(debugger_records),
        "debugger_average_success_rate": (
            round(sum(debugger_rates) / len(debugger_rates), 4) if debugger_rates else None
        ),
    }


def format_text_report(report: dict[str, Any]) -> str:
    memory_file = report.get("memory_file", {})
    size = memory_file.get("size_bytes", 0)
    lines = [
        "RBForge doctor",
        f"rbforge-version: {report['rbforge_version']}",
        f"rbmem-version: {report['rbmem_cli_version']}",
        f"memory: {report['memory_path']}",
        f"memory-health: {memory_file.get('health', 'unknown')} ({size} bytes)",
        f"registry-size: {report['registry_size']}",
        f"forged-tools: {report['forged_tools']}",
        f"validated-tools: {report['validated_tools']}",
        f"validation-rate: {_format_percent(report.get('validation_rate'))}",
        f"average-success-rate: {_format_percent(report.get('average_success_rate'))}",
        f"debugger-tools: {report.get('debugger_tools', 0)}",
        f"debugger-validation-rate: {_format_percent(report.get('debugger_validation_rate'))}",
        (
            "debugger-average-success-rate: "
            f"{_format_percent(report.get('debugger_average_success_rate'))}"
        ),
    ]
    if not memory_file.get("exists_before_check", True) and memory_file.get("exists"):
        lines.append("note: memory file was created during the check")
    return "\n".join(lines)


def run_doctor(args: argparse.Namespace) -> dict[str, Any]:
    store = RbmemStore(args.memory_path, rbmem_cli=args.rbmem_cli)
    return build_report(store)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rbforge doctor")
    add_doctor_arguments(parser)
    args = parser.parse_args(argv)
    report = run_doctor(args)
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_text_report(report))
    return 0


def _read_tool_records(
    store: RbmemStore,
    registry: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_sections: set[str] = set()
    try:
        payload = store.context("tools custom", resolve=True, minified=False, graph_depth=1)
    except Exception:  # noqa: BLE001 - diagnostics should still use registry fallback
        payload = {"sections": []}

    for section in payload.get("sections", []):
        path = section.get("path")
        if not isinstance(path, str) or not path.startswith("tools.custom."):
            continue
        content = _json_content(section.get("content"))
        if not isinstance(content, dict):
            continue
        record = dict(content)
        record.setdefault("_section_path", path)
        records.append(record)
        seen_sections.add(path)

    for item in registry:
        if not isinstance(item, dict):
            continue
        section = item.get("section")
        if not isinstance(section, str):
            name = item.get("name")
            section = f"tools.custom.{name}" if isinstance(name, str) else ""
        if section in seen_sections:
            continue
        record = dict(item)
        record["_section_path"] = section
        record["_registry_entry"] = True
        records.append(record)

    return records


def _json_content(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _is_validated(record: dict[str, Any]) -> bool:
    status = str(record.get("status", "")).lower()
    if status in {"validated", "registered"}:
        return True
    return bool(record.get("_registry_entry")) and status in {"", "validated", "registered"}


def _is_debugger_tool(record: dict[str, Any]) -> bool:
    category = str(record.get("category", "")).lower()
    dependencies = record.get("dependencies", [])
    if isinstance(dependencies, str):
        dependency_text = dependencies.lower()
    elif isinstance(dependencies, list):
        dependency_text = " ".join(str(item).lower() for item in dependencies)
    else:
        dependency_text = ""
    return category == "debugger" or "debugger" in dependency_text


def _success_rates(records: list[dict[str, Any]]) -> Sequence[float]:
    rates: list[float] = []
    for record in records:
        metrics = record.get("metrics")
        raw_rate = metrics.get("success_rate") if isinstance(metrics, dict) else record.get(
            "success_rate"
        )
        if isinstance(raw_rate, int | float):
            rates.append(float(raw_rate))
    return rates


def _category_metrics(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    categories: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        category = str(record.get("category") or "unknown")
        categories.setdefault(category, []).append(record)
    return {
        category: {
            "tools": len(items),
            "validated_tools": sum(1 for item in items if _is_validated(item)),
            "validation_rate": _validation_rate(items),
            "average_success_rate": _average_success_rate(items),
        }
        for category, items in sorted(categories.items())
    }


def _validation_rate(records: list[dict[str, Any]]) -> float | None:
    if not records:
        return None
    return round(sum(1 for record in records if _is_validated(record)) / len(records), 4)


def _average_success_rate(records: list[dict[str, Any]]) -> float | None:
    rates = list(_success_rates(records))
    return round(sum(rates) / len(rates), 4) if rates else None


def _memory_health(doctor_payload: dict[str, Any]) -> str:
    hermes_load = doctor_payload.get("hermes_load")
    if isinstance(hermes_load, dict) and isinstance(hermes_load.get("status"), str):
        return hermes_load["status"]
    status = doctor_payload.get("status")
    if isinstance(status, str):
        return status
    validation = doctor_payload.get("validation")
    if isinstance(validation, dict) and isinstance(validation.get("status"), str):
        return validation["status"]
    return "unknown"


def _format_percent(value: Any) -> str:
    if not isinstance(value, int | float):
        return "n/a"
    return f"{value * 100:.1f}%"


if __name__ == "__main__":
    raise SystemExit(main())
