"""Generate docs/DEPENDENCY_GRAPH.md: ``python -m shutterstorm.depgraph``

Everything is derived, not hand-drawn:
1. the Immich system graph from the latest real run's discovered topology (runs/*/topology.json),
2. the ShutterStorm module graph from the actual ``import`` statements (AST, incl. lazy imports),
3. the runtime graph: which ShutterStorm module talks to which external system.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

PKG = Path(__file__).resolve().parent
HOME = PKG.parent

# Which modules touch which external system (checked against the code: subprocess docker calls in
# dockerctl.py, requests to Immich in immich.py, requests to Mistral in llm.py, http.server in dashboard/sim).
EXTERNAL = {
    "dockerctl": ("docker", "Docker CLI (subprocess)"),
    "immich": ("immichapi", "Immich REST API :2283"),
    "llm": ("mistral", "Mistral API (HTTPS)"),
    "dashboard": ("browser", "Browser (127.0.0.1:8765)"),
}


def module_imports() -> dict[str, set[str]]:
    """Internal imports per module, from the AST (top-level and inside functions)."""
    graph: dict[str, set[str]] = {}
    for path in sorted(PKG.glob("*.py")):
        name = path.stem
        if name in ("__init__", "depgraph"):
            continue
        deps: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                if node.module:
                    deps.add(node.module.split(".")[0])
                else:
                    deps.update(alias.name for alias in node.names)
        graph[name] = {d for d in deps if d != name and (PKG / f"{d}.py").exists()}
    return graph


def module_mermaid(graph: dict[str, set[str]]) -> str:
    layers = {
        "CLI and UI": ["__main__", "commands", "dashboard"],
        "Orchestration": ["workflow", "experiment"],
        "AI": ["agents", "llm", "fixes"],
        "Measurement and analysis": ["probes", "scoring", "topology", "report", "reporting_io"],
        "Safety": ["faults", "journal", "guard", "safety", "checkpoint"],
        "Infrastructure": ["dockerctl", "immich", "context", "config", "sim"],
    }
    lines = ["flowchart TB"]
    placed = set()
    for i, (title, mods) in enumerate(layers.items()):
        lines.append(f'  subgraph L{i}["{title}"]')
        for m in mods:
            if m in graph:
                lines.append(f'    {m.strip("_") or m}["{m}.py"]')
                placed.add(m)
        lines.append("  end")
    for m in sorted(set(graph) - placed):
        lines.append(f'  {m.strip("_")}["{m}.py"]')
    for src in sorted(graph):
        for dst in sorted(graph[src]):
            lines.append(f"  {src.strip('_')} --> {dst.strip('_')}")
    return "\n".join(lines)


def runtime_mermaid() -> str:
    lines = ["flowchart LR",
             '  user(["Operator: CLI or dashboard"])',
             '  subgraph SS["ShutterStorm (python -m shutterstorm)"]',
             '    workflow["workflow.py"]; probes["probes.py"]; faults["faults.py"]; agents["agents.py + llm.py"]',
             '    guard["guard.py (detached process)"]; journal[(".shutterstorm/journal.json")]; report["report.py"]',
             "  end",
             '  subgraph SB["Sandbox: Docker compose project immich"]',
             '    server["immich_server :2283"]; ml["immich_machine_learning"]; redis[("immich_redis")]; db[("immich_postgres")]',
             "  end",
             '  mistral[("Mistral API")]; runs[("runs/<id>/ report.html, findings.json")]',
             "  user --> workflow",
             '  workflow --> probes -->|"HTTP probes J0-J6"| server',
             '  workflow --> faults -->|"docker pause/stop/restart/update (scope-checked)"| SB',
             '  faults -->|"write-ahead undo recipe"| journal',
             '  guard -->|"reads"| journal',
             '  guard -->|"undo if owner died / TTL passed"| SB',
             '  workflow --> agents -->|"evidence only, secrets redacted"| mistral',
             "  workflow --> report --> runs",
             "  server --> db",
             "  server --> redis",
             '  server -.->|"HTTP :3003 (not declared in compose)"| ml']
    return "\n".join(lines)


def immich_mermaid() -> tuple[str, str]:
    """From the most recent real run's discovered topology."""
    for d in sorted((HOME / "runs").glob("*-run"), reverse=True):
        topo_file = d / "topology.json"
        if topo_file.exists() and not (d / "SIMULATED.txt").exists():
            topo = json.loads(topo_file.read_text(encoding="utf-8"))
            if topo.get("mermaid"):
                return topo["mermaid"], d.name
    return "flowchart LR\n  none[\"run `python -m shutterstorm discover` first\"]", "(none)"


def journey_table() -> str:
    from .topology import JOURNEY_DEPS
    from .probes import JOURNEYS

    rows = ["| Journey | What it does | Needs |", "|---|---|---|"]
    rows += [f"| {j} | {JOURNEYS[j]} | {', '.join(JOURNEY_DEPS[j])} |" for j in JOURNEYS]
    return "\n".join(rows)


def main() -> None:
    graph = module_imports()
    immich, source_run = immich_mermaid()
    edge_count = sum(len(v) for v in graph.values())
    text = f"""# Dependency graphs

Generated by `python -m shutterstorm.depgraph` (do not edit by hand). Diagrams are Mermaid
(GitHub renders them; the dashboard shows them at http://127.0.0.1:8765/graph).

## 1. Immich system under test (discovered, not hand-drawn)

Source: `runs/{source_run}/topology.json`, built by `discover` from three sources:
solid = `compose` (depends_on), dashed = `config` (Immich's live `machineLearning.urls`,
DB/Redis host settings), thick = `empirical` (a journey broke when that service was faulted).
The server -> ML dependency is **not declared in compose**; it is only visible in config and in experiments.

```mermaid
{immich}
```

Which user journey needs which service (used for the deterministic impact prediction):

{journey_table()}

## 2. Runtime: how ShutterStorm interacts with the sandbox

```mermaid
{runtime_mermaid()}
```

## 3. ShutterStorm code: module import graph ({len(graph)} modules, {edge_count} internal imports)

Derived from the `import` statements of every module in `shutterstorm/` (including imports inside
functions). An arrow `a --> b` means `a.py` imports `b.py`.

```mermaid
{module_mermaid(graph)}
```

### Module list

| Module | Imports | External system |
|---|---|---|
""" + "\n".join(
        f"| `{m}.py` | {', '.join(sorted(graph[m])) or '-'} | {EXTERNAL.get(m, ('', '-'))[1]} |" for m in sorted(graph)
    ) + "\n"
    out = HOME / "docs" / "DEPENDENCY_GRAPH.md"
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(graph)} modules, {edge_count} imports, Immich topology from {source_run})")


if __name__ == "__main__":
    main()
