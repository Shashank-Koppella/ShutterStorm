# Live demo (about 6 minutes, presenting in person)

For the recorded 3-minute video see [DEMO_VIDEO.md](DEMO_VIDEO.md).

**Before you present (5 min early):**
1. Docker Desktop running, Immich healthy: `python -m shutterstorm doctor` (all `[OK]`).
2. Start the UI in its own PowerShell window and leave it open:
   `cd C:\Users\Shashank\Desktop\ShutterStorm; python -m shutterstorm dashboard`
3. Browser tab 1: http://127.0.0.1:8765 (dashboard). Tab 2: http://localhost:2283 (Immich, logged in;
   the 100 sample photos are in the timeline). Tab 3: http://127.0.0.1:8765/graph (dependency graphs).
4. Optional for a stable AI text: set `$env:LLM_MODE = "replay"` before starting the dashboard (uses cached
   answers; a new prompt then falls back to templates).

| Step | Show | Say |
|---|---|---|
| 1 Problem (30 s) | Dashboard header | "Cloud apps rarely fail cleanly. ShutterStorm injects controlled failures into our own Immich sandbox, measures what users feel, explains it with AI and suggests a fix, without ever applying it." |
| 2 Dependency graph (45 s) | Tab 3, diagram 1 | "Compose says the server needs Postgres and Redis. Our discovery read Immich's live config and found a dependency compose doesn't declare: the server calls the ML service over HTTP. Diagram 2 shows how our tool touches the sandbox; diagram 3 is generated from our own code's imports." |
| 2b AI decisions (45 s) | Dashboard: **Plan only (dry run)**, then the latest run's "AI decisions" table | "The AI decides what to break, when and how safely: a tentative plan from the map, then before every experiment it re-decides from what it saw, picks the fault strength and duration, and tightens the safety limits. The controller clamps everything; limits can only get stricter." |
| 3 Inject (1 min) | Dashboard: choose `ml_pause`, click **Inject fault**. Containers card: ML turns **PAUSED**; journal shows the active fault. Live log: `J3=hung`, others `ok`. Switch to Immich tab, search a new word: spinner hangs; timeline still works | "We freeze the ML container. Every fault is journaled first, has a 60 s TTL and an independent guard process. Smart search doesn't error, it just hangs." |
| 4 Recovery (20 s) | Log: `undo ... verified`, recovery round all ok; ML back to running | "After ~35 s the fault is undone and verified automatically." |
| 5 Result + suggestion (1 min) | Dashboard "Latest result": score, heatmap (J3 HUNG), AI explanation, **Suggested fix** (second ML instance + both URLs + faster health checks) and the code-level fix | "The score comes from a formula, not the AI. The AI explains and suggests a fix. It's a suggestion only: checked against allowed settings, never applied. In earlier experiments this failover idea helped but wasn't reliable, which is exactly why a human decides." |
| 6 Report (30 s) | Click **full report**: ranked findings, heatmap of all faults (open the 15:34 run for 5 faults), cleanup verification all green | "Every run proves it cleaned up: containers healthy, config unchanged, our test photos deleted." |
| 6b Auto-rollback (40 s) | Tick **strict safety limits**, inject `db_pause`: log shows `SAFETY THRESHOLD BREACHED ... Rolling back now`, result **AUTO-STOPPED** | "Limits on error rate, latency and blast radius are checked after every probe round. A breach rolls the fault back immediately, before the TTL." |
| 7 Safety (optional, 30 s) | Terminal: `python -m shutterstorm drill` | "If ShutterStorm itself crashes mid-fault, the guard restores the system on its own." |
| 8 Before/after (optional) | Report of a `run --compare-to` run: Before / after table | "After a human applies a suggested fix, we re-run the same experiments and the report shows the score change per fault." |

If anything goes wrong live: click **Panic** (or run `python -m shutterstorm panic`), then show the
report of an earlier run from the "All runs" table.
