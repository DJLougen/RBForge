# RBForge

![RBForge runtime tool creation card](assets/RBForge-card.png)

RBForge is a runtime tool creation system for Ornstein/Hermes/SABER
agents. An agent can notice a missing capability inside `<think>...</think>`,
call the `forge_tool` meta-tool, validate a proposed implementation, persist it
to Rust-Brain `.rbmem`, and reuse the new tool in the same session.

The durable memory model is the core difference from ordinary tool-use agents:
forged tools live at stable dotted RBMEM paths such as
`tools.custom.rank_lock_hotspots`, are indexed by `tools.registry`, and carry
explicit graph edges like `depends_on`, `registered_in`, `categorized_as`, and
`used_in`. RBMEM CLI updates own the temporal fields, so model-generated
timestamps are never trusted.

## Install

```shell
cd /path/to/RBForge
python -m pip install -e .[dev]
```

RBForge uses the Rust-Brain `rbmem` CLI. If it is not on PATH, set:

```shell
export RBMEM_CLI=/path/to/rbmem
```

If no CLI is found, `RBForge.forge_tool` can clone and build
`https://github.com/DJLougen/Rust-Brain` with Cargo.

## Run The Demo

```powershell
$env:PYTHONPATH = "src"
python scripts/demo_invention_loop.py
```

The demo simulates an Ornstein trace:

1. `<think>` detects that debugger + ripgrep are not enough.
2. A Hermes JSON tool call invokes `forge_tool`.
3. RBForge writes `tools.custom.rank_lock_hotspots` into RBMEM.
4. The generated tests run in Docker when available, otherwise in a restricted
   subprocess with timeout and POSIX resource limits where supported.
5. The tool is registered with graph edges and immediately reused.
6. The trace is written to `data/traces/demo_invention_loop.jsonl` for DDM.

## Autonomous Hermes Connection

RBForge is installed into Hermes in local agent configuration:

- Hermes config: `$HERMES_HOME/config.yaml` or `~/.hermes/config.yaml`
- Hermes memory: `$HERMES_HOME/MEMORY.rbmem` or `~/.hermes/MEMORY.rbmem`
- Optional WSL Hermes config, when running from Windows with WSL available:
  `~/.hermes/config.yaml` inside the selected WSL distribution

The Hermes agent registers two native tools through `tools.registry`:

- `forge_tool`
- `run_forged_tool`

The active Hermes toolsets include `RBForge`, and the `RBForge` skill tells
Hermes when to invent a tool autonomously. In normal use, start Hermes with the
skill/toolset available:

```shell
hermes -s RBForge
```

Or use the existing `hermes-cli` toolset; `forge_tool` and `run_forged_tool` are
now included there too. When Hermes identifies a reusable missing capability, it
can call `forge_tool`, then immediately call `run_forged_tool` after registration.

Reinstall or repair the bridge with:

```shell
export PYTHONPATH=src
python scripts/install_hermes_bridge.py
```

## Public API

```python
from RBForge import forge_tool, run_forged_tool

forge_result = forge_tool(
    name="count_tracebacks",
    description="Count Python tracebacks in a log.",
    schema={
        "type": "object",
        "properties": {"log": {"type": "string", "default": "Traceback"}},
        "required": ["log"],
    },
    implementation="def run(log: str) -> dict:\n    return {'count': log.count('Traceback')}\n",
    category="debugger",
    dependencies=["tools.builtin.ripgrep"],
)

reuse_result = run_forged_tool(
    name="count_tracebacks",
    arguments={"log": "Traceback\nboom"},
)
```

Both calls return clean JSON-serializable dictionaries suitable for Hermes tool
observations and DDM trajectory storage.

## Files

- `src/RBForge/forge_tool.py`: complete Ornstein-facing meta-tool wrapper.
- `examples/rbmem_tools_schema.rbmem`: full `tools.*` namespace example with a
  profiler tool and a `web_bubble` tool.
- `configs/unsloth_RBForge_sft_rl.yaml`: SFT + GRPO config with
  tool-invention rewards, task rewards, and format rewards.
- `scripts/demo_invention_loop.py`: mini invention loop with before/after
  metrics.
- `src/ornstein_rbforge/*`: earlier lower-level prototype modules kept for
  compatibility with existing examples/tests.

## Test

```shell
export PYTHONPATH=src
ruff check .
python -m compileall -q src tests examples scripts
pytest -q
graphify update .
```

After code changes, `graphify update .` keeps `graphify-out/` current.
