# ShutterStorm findings report: Immich v3.3.1

**Source run:** `runs/20261010-211247-run` (real Docker + real Immich, 10 Oct 2026, 21:12-21:19 local),
command `python -m shutterstorm run` (AI-planned, budget 6) with live Mistral (`ministral-8b-latest`;
13 live answers, 5 cached, 1 template fallback).
**Overall resilience score: 81.5 / 100** (criticality-weighted mean of the six experiments).
**Safety:** 1 of 6 experiments auto-stopped by the threshold monitor (`db_pause`, after 16 s of a 35 s window).
**Cleanup verification: PASS** (18/18 checks: containers healthy and unpaused, CPU limits restored, no helpers,
system-config identical to the checkpoint, IMMICH_DIR unchanged, probe assets deleted, journal empty).

How this document was written: every number comes from `findings.json` / `evidence/*/evidence.json` of that
run. Interpretations are marked **verified** (checked in data or logs), **observed** (measured, cause not
established) or **hypothesis**. The LLM-written text in that run's `report.html` was reviewed line by line;
the statements it got wrong are listed at the end and are **not** reproduced here as fact.

## Headline

**A hung dependency is handled much worse than a down one.** When Postgres, Redis or the ML service is
*frozen*, requests that need it wait until our 10 s client timeout without any error. Uploads during a Redis
fault hang yet are stored ("ghost uploads"). A *restarted* server or a *stopped* ML service fails fast
with a clear error. Redis is the exception: it hangs even when cleanly stopped.

## 1. Experiment plan (AI planner, live)

The Planner ranked the 14-fault catalog on the discovered dependency map and chose these 6. The strategy it
stated: *"Prioritize high-priority, high-blast-radius, and never-tested failures first, then introduce
combined failures to validate recovery mechanisms."* The validator accepted the plan: catalog faults only,
5 failure classes, every service covered, ≤ 2 per service, 1 combo.

| # | Experiment | Failure class | Deterministic priority | Planner's reason (verbatim, shortened) |
|---|---|---|---|---|
| 1 | server_restart | container crash | 1.0 | highest criticality × likelihood and widest blast radius (all journeys) |
| 2 | db_pause | hung dependency | 0.825 | widest blast radius, database dependency hang and recovery |
| 3 | ml_pause | hung dependency | 0.594 | undeclared dependency + hang style (silent-failure risk), journey J3 |
| 4 | redis_stop | dependency down | 0.453 | job-queue resilience, J4/J5 |
| 5 | traffic_spike | traffic spike | 0.726 | never tested before; server under load (J2, J3) |
| 6 | spike_redis_hung | combined failure | 0.65 | traffic + dependency hang together |

Not selected: the netem faults and `db_stop` (optional, never chosen automatically) and lower-priority or
per-service-limited faults (`spike_ml_down`, `ml_cpu_throttle`, `db_cpu_throttle`, `redis_pause`, `ml_stop`).
The Planner reordered the deterministic ranking (e.g. it put `redis_stop`, priority 0.453, ahead of
`traffic_spike`, 0.726, to cover the "dependency down" class); its reasons were checked for invented numbers only.

## 2. Ranked findings (ranking = scoring formula, not the LLM)

| Rank | Fault | Severity | S_e | A | L | C | I | R | B | Recovery after undo | Suggested fix (not applied) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | db_pause **(auto-stopped)** | 0.170 (high) | 66.0 | 0.2 | 1.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0.6 s | code-level: DB statement / pool timeouts, DB readiness check |
| 2 | spike_redis_hung | 0.084 (medium) | 73.7 | 0.8 | 0.313 | 1.0 | 1.0 | 1.0 | 0.0 | 0.7 s | code-level: timeout on job enqueue (config suggestion rejected) |
| 3 | redis_stop | 0.079 (medium) | 80.1 | 0.8 | 0.746 | 1.0 | 1.0 | 0.995 | 0.0 | 31.4 s | code-level: timeout on job enqueue |
| 4 | traffic_spike | 0.071 (medium) | 89.9 | 1.0 | 0.327 | 1.0 | 1.0 | 1.0 | 1.0 | 0.2 s | none needed |
| 5 | server_restart | 0.051 (medium) | 93.6 | 0.96 | 0.653 | 1.0 | 1.0 | 1.0 | 1.0 | 0.7 s | second server replica / client retry |
| 6 | ml_pause | 0.044 (medium) | 85.4 | 0.857 | 0.978 | 1.0 | 1.0 | 1.0 | 0.0 | 0.7 s | second ML instance + both URLs + faster availability checks |

`S_e = 100 × (0.30A + 0.15L + 0.20C + 0.15I + 0.10R + 0.10B)`;
`severity = (100 − S_e)/100 × criticality (DB/server 1.0, Redis 0.8, ML 0.6) × likelihood (pause/stop 0.5,
restart 0.8, load 0.7, combo 0.4)`. `ml_pause` ranks last mainly because of the 0.6 ML criticality weight, a
modelling choice; it is the most user-visible hang (see F5).

### Fault × journey heatmap

| Fault | J0 ping | J1 login | J2 browse | J3 smart search | J4 upload | J5 thumbnail | J6 integrity |
|---|---|---|---|---|---|---|---|
| db_pause (16 s, auto-stopped) | ok | **HUNG** | **HUNG** | **HUNG** | **HUNG** | **HUNG** | **HUNG** |
| spike_redis_hung | ok | ok | ok | ok | **HUNG** | **HUNG** | **HUNG** |
| redis_stop | ok | ok | ok | slow | **HUNG** | **HUNG** | **HUNG** |
| traffic_spike | ok | ok | ok | ok | ok | ok | ok |
| server_restart | fast-fail | fast-fail | fast-fail | fast-fail | fast-fail | ok | ok |
| ml_pause | ok | ok | ok | **HUNG** | ok | ok | ok |

HUNG = no response within 10 s. When the upload (J4) itself never returned, J5/J6 are marked HUNG because
there was no asset to check. "slow" = p95 above max(2 × baseline p95, baseline + 500 ms).

## 3. Findings in detail

### F1. A frozen database hangs the whole app while liveness still says "pong" (`db_pause`, high)
* **Verified (measured):** in the first probe round J1-J4 all hung (A = 0.2, B = 0.0); J0 `/api/server/ping`
  stayed OK (4.8 ms). Error rate 80 % > 75 % → the **safety monitor rolled the fault back after 16.0 s**
  instead of the planned 35 s. Recovery after unpause: 0.6 s.
* **Verified (docker inspect, API_NOTES #11):** Immich's Postgres healthcheck runs only every 300 s, so
  Docker's health status lags a frozen DB by up to 5 minutes in both directions.
* **Consequence (inference):** neither the ping endpoint nor Docker health reflects a frozen DB promptly; an
  orchestrator relying on either would keep routing users into hanging requests.
* **Recommendation (not tested):** bound DB waits in the server (connection-acquire and statement timeouts)
  so requests fail with a clear 503; add a readiness endpoint that touches the DB; shorten the Postgres
  healthcheck interval. (The Fix Advisor's config suggestion, a custom DB healthcheck command, was flagged by
  the checker because custom test commands are not an allowed setting.)

### F2. Redis down or frozen: uploads hang but are stored ("ghost uploads") (`redis_stop`, `spike_redis_hung`, medium)
* **Verified (measured):** in both experiments every J4 upload hung (3/3), yet all 3 were stored, later
  processed and SHA-1 intact (`ghost_uploads: 3`, integrity 3/3). Matches manual finding M-03.
* **Observed:** `redis_stop` behaves like a freeze, not a fast failure; recovery took 31.4 s after
  `docker start`. During `redis_stop` J3 smart search was also slow (p95 6500 ms vs 94 ms baseline). The
  cause of the J3 slowdown is **not established** (it was also seen in earlier runs: 9543 ms).
* **Hypothesis (not verified):** the server's Redis client queues commands while disconnected instead of
  rejecting them; the server log only shows `missing 'error' handler on this Redis client`.
* **Recommendation (not tested):** put a timeout on job enqueueing and return the upload response once the
  file and DB row are committed; show real per-file status in the web uploader.

### F3. Combined failure: a traffic spike on top of a frozen Redis adds latency but no new failure (`spike_redis_hung`)
* **Verified (measured):** 24 clients sent 10 548 read-only requests (243/s, 0 errors) while Redis was frozen.
  The failure set was the same as for Redis alone (J4-J6 hung); J1-J3 stayed OK but slower (p95 193 / 208 /
  287 ms vs 81 / 79 / 94 ms baseline), so the score dropped to 73.7 mainly through latency (L = 0.313).
* **Interpretation:** the two faults add up; we saw no cascade (no protected journey failed, the blast-radius
  limit was never hit).

### F4. Traffic spike alone: absorbed without errors, 3-5× slower (`traffic_spike`)
* **Verified (measured):** 7 785 requests at 198/s from 24 clients; all 126 probe calls succeeded (A = 1.0).
  p95 rose 3-5× (J2 browse 309 ms vs 79 ms, J3 smart search 409 ms vs 94 ms, J4 upload 470 ms vs 100 ms).
  Below our "slow" cut-off, so the heatmap stays OK; the latency component (L = 0.327) records it.
* **Interpretation:** on this laptop sandbox, Immich degrades gracefully under this load: a positive result.
  A larger spike may behave differently; we did not test beyond 24 clients.

### F5. Frozen ML makes smart search hang silently (`ml_pause`, medium)
* **Verified (measured):** J3 hung 4/4 calls; every other journey OK. Matches M-01 (spinner forever).
* **Suggested fix (template, not applied, not tested in this version):** add `immich-machine-learning-2`
  (same image, shared model cache), set `machineLearning.urls` to both instances and availability checks to
  1000 ms / 5000 ms. The checker confirms it only uses allowed settings. **History:** an earlier version of
  ShutterStorm applied and re-tested this idea 4 times; 2 re-tests were clean and 2 still had one hung
  smart search, so failover is helpful but not guaranteed. That is why fixes are now suggestions for a
  human. To measure it again after applying it by hand: `run --experiments ml_pause --compare-to 20261010-211247-run`.

### F6. Server restart: short, fast-failing outage (`server_restart`, medium)
* **Verified (measured):** J0-J4 had 1 fast failure each in 18 calls during the restart; recovery 0.7 s;
  17/18 uploads stored and intact. The safety monitor saw a breached round but, by design, does not "roll
  back" a restart (it cannot be undone early); the outage bounded itself.
* **Recommendation:** a second API server replica behind a load balancer, or client-side retry with backoff.

### Supplementary (earlier full run `20261010-210549-run`, same code except the restart rule; cleanup PASS)
* `redis_pause`: J4-J6 hung, score 82.1, i.e. the same ghost-upload pattern as F2.
* `db_cpu_throttle` (Postgres at 0.25 CPU): score 95.8, all journeys OK except **one** "Failed to search
  smart" error in 18 smart searches (observed, cause not established). Overloading the DB's CPU hurt far
  less than freezing it.

## 4. Dependency map and predictions
* **Verified:** compose declares server → database and server → redis only. The server → ML dependency is
  only in Immich's runtime config (`machineLearning.urls`); the planner flagged it as undeclared and `ml_pause`
  confirmed it empirically.
* Prediction scorecard (deterministic map + Analyst): precision 0.59, recall 0.84. Mismatches worth noting:
  `redis_stop` also slowed J3 (not predicted); the traffic spike was predicted to slow 5 journeys but none
  crossed the "slow" threshold.

## 5. Safety record of this run
Thresholds: error rate > 75 %, p95 > 8000 ms, or a journey outside the blast radius fails (1 round). Six
experiments (7 atomic faults: the combo has two journal entries) were applied and undone, each verified
before the next; `db_pause` was **auto-stopped** at 16 s; no TTL or guard intervention was needed; the
traffic generator stopped with each undo; final cleanup 18/18 checks passed. (The dead-man guard is exercised by `python -m shutterstorm drill`
and by the test suite.)

## 6. What the LLM got wrong in this run (reviewed by us)
* `ml_pause` analysis: "J4 and J5 slowed significantly (p50 91.1 ms → 86.9 ms …)": false, those numbers
  show no slowdown; J4/J5 were OK.
* `spike_redis_hung` analysis: J1-J3 "degraded to slow": they were OK by our heatmap rule (2-3× slower
  but within the threshold).
* `traffic_spike` analysis: "the ML service intermittently became unhealthy": not supported by our evidence.
* Fix Advisor for `redis_stop`: suggested changing **ML** availability checks, which is a valid setting
  but irrelevant to Redis.
* Planner: labelled `traffic_spike` as "resource exhaustion" in its reason (the catalog class is "traffic spike").

Mitigations in the code: numbers and fault names in AI text are checked against the evidence (retry, then
fallback); plans must pass the catalog/diversity validator; ranking, scores, safety decisions and pass/fail
never come from the LLM. Statement-level claims still need human review, which is why this report was
written from the data.

## 7. Manual findings (by hand, web UI)
See [MANUAL_FINDINGS.md](MANUAL_FINDINGS.md): M-01 (ML hung → spinner forever), M-02 (ML down → error toast +
misleading "No results"), M-03 (Redis hung → upload stuck at "Processed 0/1", asset stored). M-01 and M-03 are
reproduced by this run (F5, F2); M-02 by earlier runs (`ml_stop`: J3 fast-fail on every call).
