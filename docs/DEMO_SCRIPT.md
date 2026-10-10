# Demo video script (≤ 3:00)

**Before recording:**
* Run `python -m shutterstorm doctor` (all OK).
* Do one full real run with `LLM_MODE=live` so the cache is warm, then record with `LLM_MODE=replay`
  (identical AI text, no rate limits, no network surprises).
* Layout: terminal on the left, Immich web UI (`http://localhost:2283`) on the right, the previous run's
  `report.html` in a second browser tab.
* Font size ≥ 18 pt. Hide `shutterstorm.env`.

| Time | Segment | On screen | Say |
|---|---|---|---|
| 0:00-0:15 | **Problem** | Title slide / README headline | "Cloud apps rarely fail cleanly. ShutterStorm is an AI chaos engineer for Immich, a self-hosted photo app with 4 services, all in our own sandbox." |
| 0:15-0:35 | **Dependency map** | `python -m shutterstorm discover` → Mermaid map (topology.md preview) | "Compose says the server needs Postgres and Redis. Our discovery reads Immich's live config and finds a dependency compose doesn't declare: the server calls the ML service over HTTP. Experiments later confirm it empirically." |
| 0:35-1:15 | **Live fault + kill switch** | `python -m shutterstorm inject ml_pause --duration 30`; in the UI type a new smart-search query → spinner hangs; timeline still works. Terminal shows `J3=hung`, others ok. Then Ctrl+C (or let the TTL expire) → `undo verified` → cleanup PASS | "We freeze the ML container. Every write is journaled first, a TTL and an independent guard process guarantee it gets undone. Smart search doesn't error, it just hangs. That's the worst kind of failure for a user." |
| 1:15-2:05 | **AI analysis + repair loop** | Show the end of a `run --repair` log: predicted vs actual, the Analyst's explanation, the repair patch (second ML instance + both URLs + faster availability checks), protected tests, **same fault re-injected → J3 hung → ok, score 85.1 → 97.3 (+12.2)**, patch reverted. Also show the validator rejecting an LLM patch (`immich-server.environment is not allowed`) | "The Repair agent proposes fixes using only whitelisted knobs; if its answer is invalid, a deterministic fallback proposes one. Code, not the AI, decides whether it worked: it re-injects the same fault. Here smart search survives the frozen ML. The validator blocked AI patches outside the allowed knobs. For Redis there's no config fix, so it's reported as unresolved." (Say honestly that the verified ML patch in our run came from the fallback.) |
| 2:05-2:45 | **Report + score** | `report.html`: overall score, ranked findings, heatmap (HUNG vs fast-fail cells), prediction scorecard, cleanup verification all green, AI-source badges | "Severity comes from a formula, not the LLM. The headline: a hung dependency is handled much worse than a down one. Uploads during a Redis freeze hang for the user but are actually stored: ghost uploads." |
| 2:45-3:00 | **Guard drill** | `python -m shutterstorm drill` → `KILLING main process` → `[guard] taking over` → `GUARD DRILL PASSED` | "And if ShutterStorm itself crashes mid-fault, the dead-man guard restores the system on its own." |

Tips: cut waiting time (recovery waits, healthchecks) in editing; keep the terminal timestamps visible so
the cuts are honest.
