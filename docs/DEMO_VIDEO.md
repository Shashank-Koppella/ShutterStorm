# Demo video (3:00 max): shot list and narration

The video must show **injecting a failure, observing the impact, and reporting findings**. Record the
screen with Clipchamp (built into Windows 11; records the whole screen, unlike Game Bar) or OBS at 1080p; keep the browser zoom at 110 % so text is
readable. Narration is ~360 words, so speak at a calm pace. Cut waiting time (fault windows, recovery)
in editing; never speed up text the viewer has to read.

## Before recording
1. `python -m shutterstorm doctor`: every line `[OK]`.
2. `$env:LLM_MODE = "replay"` (cached AI answers, stable text), then `python -m shutterstorm dashboard`.
3. Browser tabs: (1) the dashboard http://127.0.0.1:8765 (Run results of the final run `20261011-003228-run`),
   (2) http://localhost:2283 logged in. Everything else is a screen of the dashboard; do not press Stop during a run.
4. Terminal window visible next to the browser (large font).

## Shots

| Time | Screen | Narration |
|---|---|---|
| 0:00-0:15 | Dashboard **Run results** (matrix visible), then Immich timeline in tab 2 | "This is ShutterStorm. It runs controlled chaos experiments on Immich, an open-source, multi-service photo app, inside our own Docker sandbox, and tells us how it breaks and how to fix it." |
| 0:15-0:40 | Dashboard **Dependency graph**: the dashed Machine learning node is selected; point at "Runtime configuration" vs "No depends_on in Compose" | "First it maps the system. Compose declares that the server needs Postgres and Redis. By reading Immich's live configuration, ShutterStorm found a dependency compose does not declare: the server calls the machine-learning service over HTTP." |
| 0:40-1:05 | **Experiment plan** → **Generate plan**: ordered faults with the AI's reasons and the priority bars; then **AI decisions** for the final run: "changed plan", tightened limits, clamps | "An AI planner uses that map to decide what to break, when, and how safely. It ranks our fixed catalog by criticality, blast radius and hidden dependencies, and before every experiment it decides again from what it has just seen: which fault next, how hard, for how long, and how strict the safety limits are. Code clamps every value, and the AI can only make limits stricter, never looser." |
| 1:05-1:45 | Experiment plan → Manual injection → `ml_pause` → **Inject fault**; the app opens **Live run**: top bar shows ML **paused**, the J3 tile turns **Hung** while the rest stay OK, the J3 strip climbs to the timeout. Switch to tab 2, smart search a new word: spinner never ends; timeline still works | "We freeze the ML container. Every fault is journaled first, has a 60-second TTL and an independent guard process. Smart search doesn't error, it just hangs, while the rest of the app keeps working. After 35 seconds the fault is undone and verified automatically." |
| 1:45-2:10 | Manual injection, tick **Strict limits**, inject `db_pause`. On Live run the error-rate meter crosses its limit and the "Automatic rollback" notice appears; then **Safety record**: breach → rollback → recovery | "Safety limits are checked after every probe round. Here we freeze the database: the error rate crosses the threshold, and ShutterStorm rolls the fault back on its own, long before the time limit." |
| 2:10-2:50 | **Run results** of the final run: 85.2 score, matrix, select `db_pause`: measured effect → safety response → fix marked "Not applied · Not tested"; scroll to the latency strips; **Safety record** cleanup 18/18 | "The report ranks the weaknesses with a fixed formula, not the AI, and gives a resilience score. The worst one: when a dependency hangs, users get no error at all; when it's down, they get a fast failure. The AI explains each finding and suggests a fix, which is checked against allowed settings and never applied: a human decides. Every run ends by proving cleanup: containers healthy, configuration unchanged, test photos deleted." |
| 2:50-3:00 | Dashboard header | "ShutterStorm: AI-planned, safety-first chaos engineering. Thank you." |

## After recording
* Check the length is ≤ 3:00 and the text in the log and report is readable at 720p.
* Export MP4 (H.264), upload, and put the link in the submission form and at the top of the README.
