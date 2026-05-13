from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from rbforge_core.version import check_rbmem_compatibility


def test_local_rust_brain_cli_reports_compatible_version() -> None:
    configured = os.environ.get("RBMEM_CLI")
    if configured:
        rbmem = Path(configured)
    else:
        rbmem = (
            Path(__file__).resolve().parents[2]
            / "Rust-Brain"
            / "target"
            / "debug"
            / ("rbmem.exe" if os.name == "nt" else "rbmem")
        )
    if not rbmem.exists():
        pytest.skip("local Rust-Brain debug rbmem binary is not built")

    completed = subprocess.run(
        [str(rbmem), "--version"],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    assert check_rbmem_compatibility(completed.stdout).ok
