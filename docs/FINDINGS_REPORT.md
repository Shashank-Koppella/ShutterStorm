# ShutterStorm findings report: Immich v3.3.1

**Source run:** `runs/20261011-003228-run` (real Docker + real Immich, 11 Oct 2026, 00:32-00:42 local),
command `python -m shutterstorm run` (budget 6) with live Mistral (`ministral-8b-latest`; 26 API calls:
18 live answers, 6 cached, 1 template fallback).
**Overall resilience score: 85.2 / 100** (criticality-weighted mean of the six experiments).
**The AI decided what, when and how safely:** before each experiment it chose the next fault from what
the run had shown so far (it changed its own tentative plan in 5 of 6 steps), the fault strength, the
duration and stricter safety limits. **2 of 6 experiments were rolled back early** by those limits.
**Cleanup verification: PASS** (18/18 checks).

How this document was written: every number comes from `findings.json` / `evidence/*/evidence.json` of
that run. Interpretations are marked **verified** (checked in data or logs), **observed** (measured, cause
not established) or **hypothesis**. The LLM-written text in the run's `report.html` was reviewed line by
line; statements it got wrong are listed in section 6 and are not reproduced here as fact.

## Headline

**A hung dependency is handled much worse than a down one.** When Postgres or Redis is *frozen*,
requests that need it wait until our 10 s client timeout without any error, and uploads hang yet are
stored ("ghost uploads"). A *restarted* API server fails fast with clear errors. Overloading a
dependency's CPU (down to 0.1 CPU) hurt far less than freezing it.

## 1. How the AI ran the experiments

Tentative plan (live): db_pause, ml_pause, redis_stop, server_restart, ml_cpu_throttle, spike_redis_hung.
Before each step the planner saw the results so far and decided again:

| Step | Experiment (what) | Plan | Strength and duration | Safety limits (AI, clamped) | Outcome |
|---|---|---|---|---|---|
| 1 | db_cpu_throttle | changed (was db_pause) | Postgres at **0.1 CPU**, 45 s | errors > 25 %, p95 > 2,000 ms | no failures; score 99.4 |
| 2 | redis_pause | changed | 35 s | 25 %, 2,000 ms | uploads hang; score 84.0 |
| 3 | spike_redis_hung | changed | 24 clients, 30 s | 25 %, 2,000 ms (asked 15 %, 1,500 ms) | uploads hang under load; score 76.3 |
| 4 | ml_cpu_throttle | changed | ML at **0.1 CPU**, 30 s | 25 %, 2,000 ms | **AUTO-STOPPED after 5.4 s** (p95 2,200 ms) |
| 5 | db_pause | as planned | 30 s | 25 %, 2,000 ms (asked 15 %, 1,500 ms) | **AUTO-STOPPED after 16.3 s** (80 % errors) |
| 6 | server_restart | changed (was ml_pause) | 20 s | 25 %, 2,000 ms (asked 15 %, 1,500 ms) | brief fast failures; score 90.9 |

* **What the controller enforced:** every choice came from the catalog; four times the AI asked for limits
  stricter than the floor (15 %, 1,500 ms) and they were clamped to 25 % / 2,000 ms; once it sent an unknown
  setting (`strength_knob`), which was ignored. Safety limits can only be stricter than the configured caps
  (75 %, 8,000 ms); the AI tightened them on every experiment.
* **Observed behaviour of the planner:** it favoured untested resource-exhaustion and combined faults first and
  ran the database hang late; it did not choose `ml_pause` this run (covered in the supplementary run below).

## 2. Ranked findings (ranking = scoring formula, not the LLM)

| Rank | Fault | Severity | S_e | A | L | C | I | R | B | Recovery after undo | Suggested fix (not applied) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | db_pause **(auto-stopped)** | 0.170 (high) | 66.0 | 0.2 | 1.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0.5 s | DB statement / pool timeouts, readiness check (code level) |
| 2 | spike_redis_hung | 0.076 (medium) | 76.3 | 0.8 | 0.485 | 1.0 | 1.0 | 1.0 | 0.0 | 0.7 s | timeout on job enqueue (code level) |
| 3 | server_restart | 0.073 (medium) | 90.9 | 0.848 | 0.698 | 1.0 | 1.0 | 1.0 | 1.0 | 8.6 s | second server replica / client retry |
| 4 | redis_pause | 0.064 (medium) | 84.0 | 0.8 | 1.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0.5 s | timeout on job enqueue (code level) |
| 5 | ml_cpu_throttle **(auto-stopped)** | 0.008 (low) | 97.8 | 1.0 | 0.864 | 1.0 | 1.0 | 0.984 | 1.0 | 34.3 s* | ML request timeout + "search unavailable" message |
| 6 | db_cpu_throttle | 0.004 (low) | 99.4 | 1.0 | 0.957 | 1.0 | 1.0 | 1.0 | 1.0 | 7.7 s | none needed |

`S_e = 100 × (0.30A + 0.15L + 0.20C + 0.15I + 0.10R + 0.10B)`;
`severity = (100 − S_e)/100 × criticality (DB/server 1.0, Redis 0.8, ML 0.6) × likelihood (pause/stop 0.5,
restart 0.8, load 0.7, cpu 0.6, combo 0.4)`.
\* The 34.3 s came from our own undo, which at that time removed a `--cpus` limit by recreating the ML
container (Docker 29.8 ignores `--cpus 0`). CPU faults now use the CFS quota and undo in place
(API_NOTES #4); recovery after that change: 0.5 s (verified with `inject db_cpu_throttle`).

### Fault × journey heatmap

| Fault | J0 ping | J1 login | J2 browse | J3 smart search | J4 upload | J5 thumbnail | J6 integrity |
|---|---|---|---|---|---|---|---|
| db_pause (16 s, auto-stopped) | ok | **HUNG** | **HUNG** | **HUNG** | **HUNG** | **HUNG** | **HUNG** |
| spike_redis_hung | ok | ok | ok | ok | **HUNG** | **HUNG** | **HUNG** |
| server_restart | fast-fail | fast-fail | fast-fail | fast-fail | fast-fail | ok | ok |
| redis_pause | ok | ok | ok | ok | **HUNG** | **HUNG** | **HUNG** |
| ml_cpu_throttle (5 s, auto-stopped) | ok | ok | ok | slow | ok | ok | ok |
| db_cpu_throttle (0.1 CPU) | ok | ok | ok | ok | ok | ok | ok |

HUNG = no response within 10 s. When the upload (J4) itself never returned, J5/J6 are marked HUNG because
there was no asset to check.

## 3. Findings in detail

### F1. A frozen database hangs the whole app while liveness still says "pong" (`db_pause`, high)
* **Verified:** in the first probe round J1-J4 all hung, J0 ping answered in 5.9 ms. Error rate 80 % > the
  AI's 25 % limit → rolled back after 16.3 s. Recovery 0.5 s after unpause.
* **Verified (docker inspect, API_NOTES #11):** Immich's Postgres healthcheck runs every 300 s, so Docker's
  health status lags a frozen database by up to 5 minutes.
* **Recommendation (not tested):** DB connection-acquire and statement timeouts in the server (fail with a
  clear 503), a readiness endpoint that touches the DB, a shorter Postgres healthcheck interval.

### F2. Redis frozen: uploads hang but are stored, also under load (`redis_pause`, `spike_redis_hung`, medium)
* **Verified:** J4 hung 3/3 in both experiments; all 3 uploads were stored and later processed with a
  matching SHA-1 (`ghost_uploads: 3`, integrity 3/3). Matches manual finding M-03.
* **Verified (combo):** 24 read-only clients sent 12,110 requests (280/s, 0 errors) while Redis was frozen.
  The failure set stayed the same (J4-J6); J1-J3 stayed OK but slower (p95 168 / 172 / 242 ms vs 87 / 78 /
  105 ms baseline). No cascade: the blast-radius check was never triggered.
* **Hypothesis (not verified):** the server's Redis client queues commands while Redis is unavailable instead
  of rejecting them (server log: `missing 'error' handler on this Redis client`).
* **Recommendation (not tested):** a timeout on job enqueueing; return the upload response once file and DB
  row are committed; show real per-file status in the uploader.

### F3. Server restart: short, fast-failing outage (`server_restart`, medium)
* **Verified:** J0-J4 each had 2 fast failures in 10 calls; 8 of 10 uploads succeeded and were intact, the 2
  that failed were never stored; recovery 8.6 s. A restart cannot be rolled back early, so the monitor only
  records breaches for it.
* **Recommendation:** a second API server replica behind a load balancer, or client retry with backoff.

### F4. CPU starvation hurts far less than a freeze (`db_cpu_throttle`, `ml_cpu_throttle`, low)
* **Verified:** Postgres at 0.1 CPU for 45 s: 161 of 161 probe calls OK, p95 within ~15 % of baseline.
  ML at 0.1 CPU: smart search p95 2,200 ms (baseline 105 ms), which crossed the AI's 2,000 ms limit, so the
  fault was rolled back after 5.4 s; nothing failed.
* **Interpretation:** with our small test library Immich's database work is light; the ML model
  is CPU-bound, so smart search slows first. A larger library may behave differently.

### Supplementary: earlier full run `20261010-211247-run` (same tool before the per-step AI decisions)
* `ml_pause`: smart search hung 4/4, everything else OK (score 85.4): the silent ML hang (M-01).
* `redis_stop`: uploads hung (ghost uploads 3/3), smart search slow (p95 6,500 ms); recovery 31.4 s.
* `traffic_spike` alone: 7,785 requests at 198/s from 24 clients, 0 errors, probe p95 3-5× baseline (score 89.9).
* Earlier runs: `ml_stop` failed fast on every smart search (score 93.3), confirming "down fails fast" for ML.

### Container verification run `20261011-010120-run`
The same workflow with the agents in their isolated container (no Docker socket, internal network, egress
only to api.mistral.ai): 3 AI-decided experiments (spike_redis_hung with 32 clients, redis_pause,
db_cpu_throttle at 0.1 CPU), overall 85.7, cleanup PASS; the egress proxy log shows every outbound connection went to
api.mistral.ai (a test request to another site was refused). One agent call failed on a malformed model answer; the controller fell back to the template
and the bug is fixed.

## 4. Dependency map and predictions
* **Verified:** compose declares server → database and server → redis only; server → ML exists only in
  Immich's runtime config (`machineLearning.urls`), confirmed empirically by `ml_pause` / `ml_cpu_throttle`.
* Prediction scorecard (map + Analyst): precision 0.55, recall 0.89: predictions rarely missed a broken
  journey but over-predicted (e.g. a database CPU limit was expected to slow everything; nothing broke).

## 5. Safety record of this run
Caps: error rate 75 %, p95 8,000 ms, blast radius, 1 round. The AI chose stricter limits for every
experiment (25 % / 2,000 ms after clamping). Two experiments were rolled back early by those limits
(`ml_cpu_throttle` at 5.4 s, `db_pause` at 16.3 s); no TTL or guard intervention was needed; every undo was
verified; cleanup 18/18. Separately, the guard was tested across containers: the owner process was killed
mid-fault and the guard container undid the fault 17 s later from the stale heartbeat.

## 6. What the LLM got wrong in this run (reviewed by us)
* Planner, step 4: "Prior Redis hangs suggest ML may be a hidden dependency under CPU pressure": no evidence links them.
* Planner, steps 3, 5, 6: its safety reasons cite 15 % / 1,500 ms, values the controller then clamped to
  25 % / 2,000 ms (the report shows both).
* `redis_pause` analysis: other journeys "exhibited minor latency increases": J1/J2 p95 were at or below baseline.
* `db_cpu_throttle` analysis: "increased latency observed in all metrics": increases were small (≤ 15 %) and not in every journey.
* Fix Advisor for `spike_redis_hung`: suggested tighter Redis healthchecks; valid settings, but they would not stop the hang.

Mitigations in the code: the controller re-checks every AI decision (catalog, selection rules, clamped
settings, stricter-only limits); numbers and fault names in AI text are checked against the evidence;
ranking, scores and safety never come from the LLM. Statement-level claims still need human review, which is
why this report was written from the data.

## 7. Manual findings (by hand, web UI)
See [MANUAL_FINDINGS.md](MANUAL_FINDINGS.md): M-01 (ML hung → spinner forever), M-02 (ML down → error toast +
misleading "No results"), M-03 (Redis hung → upload stuck at "Processed 0/1", asset stored). M-03 is
reproduced in this run (F2); M-01 and M-02 in the supplementary runs.
