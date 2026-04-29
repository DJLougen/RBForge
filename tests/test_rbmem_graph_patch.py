from __future__ import annotations

from pathlib import Path

from ornstein_rbforge.rbmem import RbmemStore


class NoopRbmemStore(RbmemStore):
    def __init__(self, memory_path: Path) -> None:
        self.memory_path = memory_path
        self.rbmem_cli = "rbmem"

    def validate(self) -> None:
        return None


def test_apply_graph_inserts_relations_without_touching_temporal(tmp_path: Path) -> None:
    memory = tmp_path / "memory.rbmem"
    memory.write_text(
        """rbmem# RBMEM v1.3 - Rust-Brain Memory Format

meta:
  version: 1.3

[SECTION: tools.custom.demo]
type: json
temporal:
  created_at: "2026-04-28T00:00:00Z"
  updated_at: "2026-04-28T00:00:00Z"
  expires_at: null
content: |
  {"name":"demo"}
[END SECTION]
""",
        encoding="utf-8",
    )

    store = NoopRbmemStore(memory)
    store.apply_graph(
        "tools.custom.demo",
        node_type="tool",
        relations=[{"to": "tools.registry", "type": "registered_in"}],
    )

    text = memory.read_text(encoding="utf-8")
    assert 'node_type: "tool"' in text
    assert 'to: "tools.registry"' in text
    assert 'updated_at: "2026-04-28T00:00:00Z"' in text
