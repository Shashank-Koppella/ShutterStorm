# ShutterStorm findings report: Immich v3.3.1

**Source run:** `runs/20261010-145857-run` (real Docker + real Immich, 10 Oct 2026, 14:58-15:18 local),
command `python -m shutterstorm run --repair` with live Mistral (`ministral-8b-latest`).
**Overall resilience score: 81.2 / 100** (criticality-weighted mean of the six experiments).
**Cleanup verification: PASS** (20/20 checks: containers healthy and unpaused, CPU limits restored, no helpers,
system-config identical to the checkpoint, override file removed, probe assets deleted, journal empty).

How this document was written: every number comes from `findings.json` / `evidence/*/evidence.json` of
that run. Interpretations are marked **verified** (checked in data or logs), **observed** (measured, cause
not established) or **hypothesis**. The LLM-written text in that run's `report.html` was reviewed line by
line; it contained several false statements (listed at the end), so it is **not** reproduced here as fact.

## Headline

**A hung dependency is handled much worse than a down one.** When a backing service is *frozen*,
requests that need it wait until our 10 s client timeout without any error; when it is cleanly
*stopped*, the ML case fails fast with an error. Redis is the exception: it hung in **both** cases.

## Ranked findings (ranking = scoring formula, not the LLM)

| Rank | Fault | Severity | S_e | A | L | C | I | R | B | Recovery after undo | Repair |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | db_pause | 0.189 (high) | 62.1 | 0.2 | 0.74 | 1.0 | 1.0 | 1.0 | 0.0 | 0.7 s | no config-level fix |
| 2 | redis_stop | 0.087 (medium) | 78.3 | 0.8 | 0.619 | 1.0 | 1.0 | 0.997 | 0.0 | 30.8 s | no config-level fix |
| 3 | redis_pause | 0.078 (medium) | 80.6 | 0.8 | 0.774 | 1.0 | 1.0 | 1.0 | 0.0 | 0.6 s | no config-level fix |
| 4 | server_restart | 0.052 (medium) | 93.5 | 0.918 | 0.728 | 1.0 | 1.0 | 1.0 | 1.0 | 0.5 s | unresolved (3 attempts) |
| 5 | ml_pause | 0.045 (medium) | 85.1 | 0.857 | 0.959 | 1.0 | 1.0 | 1.0 | 0.0 | 0.6 s | **verified patch** |
| 6 | ml_stop | 0.02 (low) | 93.3 | 0.857 | 0.843 | 1.0 | 1.0 | 0.993 | 1.0 | 32.0 s | no config-level fix |

`S_e = 100 × (0.30A + 0.15L + 0.20C + 0.15I + 0.10R + 0.10B)`;
`severity = (100 − S_e)/100 × criticality (DB/server 1.0, Redis 0.8, ML 0.6) × likelihood (pause/stop 0.5, restart 0.8)`.
ML faults rank low mainly because of the 0.6 criticality weight; that is a modelling choice, documented here.

### Fault × journey heatmap (35 s fault window each)

| Fault | J0 ping | J1 login | J2 browse | J3 smart search | J4 upload | J5 thumbnail | J6 integrity |
|---|---|---|---|---|---|---|---|
| ml_pause | ok | ok | ok | **HUNG** | ok | ok | ok |
| ml_stop | ok | ok | ok | fast-fail | ok | ok | ok |
| redis_pause | ok | ok | ok | ok | **HUNG** | **HUNG** | **HUNG** |
| redis_stop | ok | ok | ok | slow | **HUNG** | **HUNG** | **HUNG** |
| db_pause | ok | **HUNG** | **HUNG** | **HUNG** | **HUNG** | **HUNG** | **HUNG** |
| server_restart | fast-fail | fast-fail | fast-fail | fast-fail | fast-fail | ok | ok |

HUNG = no response within 10 s. For Redis and DB faults, J5/J6 are marked HUNG because the upload (J4)
itself never returned, so there was no asset to check.

## Findings in detail

### F1. Database freeze makes the whole app hang, but liveness still says "pong" (`db_pause`, high)
* **Verified (measured):** J1-J6 hung on every call (A = 0.2, B = 0.0); J0 `/api/server/ping` stayed OK
  throughout. Recovery after unpause: 0.7 s.
* **Verified (docker inspect):** Immich's Postgres healthcheck runs only every **300 s**. After the pause,
  Docker kept showing `immich_postgres` as `unhealthy` for ~5 minutes although the API worked again
  (see API_NOTES #11).
* **Consequence (inference):** neither the ping endpoint nor Docker's health status reflects a frozen DB
  promptly; a monitor relying on either would report the system healthy while users are stuck, and later
  report it unhealthy while it already works.
* **Recommendation (not tested):** bound DB waits in the server (connection-acquire and statement
  timeouts) so requests fail with a clear 503; add a readiness check that touches the DB; shorten the
  Postgres healthcheck interval.

### F2. Redis down or frozen: uploads hang but are stored ("ghost uploads") (`redis_stop`, `redis_pause`, medium)
* **Verified (measured):** in both experiments J4 upload never returned within 10 s (2/2 calls hung each),
  yet afterwards both uploads existed on the server (`ghost_uploads: 2`) and were later processed with a
  matching SHA-1 (integrity 2/2). This matches manual finding M-03 ("Processed 0/1", asset appears after refresh).
* **Observed:** `redis_stop` behaved like a freeze, not like a fast failure; recovery took 30.8 s after
  `docker start`. During `redis_stop`, J3 smart search was also slow (9543 ms vs 149.4 ms baseline p95,
  2 calls). Cause of the J3 slowdown: **not established**.
* **Hypothesis (not verified):** the server's Redis client queues commands while disconnected instead of
  rejecting them; the server log only shows `missing 'error' handler on this Redis client`.
* **Recommendation (not tested):** put a timeout on job enqueueing and return the upload response once the
  file and DB row are committed; show real per-file status in the web uploader.

### F3. Frozen ML makes smart search hang; a second ML instance fixes it (`ml_pause`, medium, **verified fix**)
* **Verified (measured):** J3 hung 4/4 calls; all other journeys OK; smart-search/face/OCR queues
  backed up to 4 jobs. Matches M-01.
* **Verified fix (repair loop, attempt 1):** add `immich_machine_learning_2` (same image, shared model cache),
  set `machineLearning.urls` to both instances and `availabilityChecks` to timeout 1000 ms / interval 5000 ms.
  With the **same fault re-injected** on the original ML container: J3 hung → ok, score 85.1 → 97.3
  (+12.2); protected journeys passed; the only file changed in IMMICH_DIR was
  `docker-compose.shutterstorm.yml`. The patch was then reverted.
* **Honest caveats:** (1) this patch came from the **deterministic fallback**, not the LLM: the LLM's
  answers failed schema validation twice. (2) In an earlier run (14:42) the same patch failed once: after
  ~40 s of successful failover one J3 call hung again, so failover is intermittent rather than guaranteed.
  It costs ~1-2 GB extra RAM.

### F4. ML stopped: fast, clear failure (`ml_stop`, low)
* **Verified (measured):** J3 failed fast on 15/15 calls (B = 1.0); everything else OK. Recovery 32.0 s
  (ML container start-up until healthy). Matches M-02; the UI's "No results, try a synonym" page after the
  error toast (M-02, manual) is misleading.
* **Repair:** the failover patch made J3 pass (+4.0 and +4.2), but missed the +10 score bar because the
  baseline score was already 93.3. Recorded as not verified; note that the +10 bar cannot be met by faults
  that already score above 90.

### F5. Server restart: short fast-fail outage (`server_restart`, medium)
* **Verified (measured):** J0-J4 had fast failures during the restart (e.g. J3 and J4: 2 of 18 calls each); recovery 0.5 s;
  16/18 uploads stored and intact.
* **Repair:** no config fix; a second server replica would be needed (outside our allowed knobs).
  One attempt was wrongly failed by **our own tool** (health wait shorter than Postgres's 300 s healthcheck
  interval, now fixed).

## Dependency map and predictions
* **Verified:** compose declares server → database and server → redis only. The server → ML dependency
  appears only in Immich's runtime config (`machineLearning.urls`) and was confirmed empirically (J3 broke
  under both ML faults).
* Prediction scorecard (deterministic map + Analyst): precision 0.7, recall 0.8. Mismatches worth noting:
  `redis_stop` also slowed J3 (not predicted); `server_restart` hit J0 but not J5/J6.

## Safety record of this run
Six faults applied and undone, each verified before the next; the fail-closed validator rejected two LLM
patches (custom server `environment`; a custom healthcheck `test` command); every applied patch was
reverted and verified; final cleanup 20/20 checks passed.

## What the LLM got wrong in this run (reviewed by us)
Statements from the `ministral-8b` Analyst/Reporter that the data contradicts or does not support:
* "Redis … exhibit isolated fast-fail patterns": false; both Redis faults hung.
* `db_pause`: "foreign key errors post-fault indicated orphaned records from interrupted transactions":
  false; those errors come from our cleanup deleting probe assets while their jobs were queued (verified in logs).
* `redis_pause`: "ML errors (J3) … cascaded from network instability": false; J3 was OK in that experiment.
* `ml_pause`: "J4 and J5 degraded … server implemented retries": not supported; J4/J5 were OK and nothing
  shows retries.
* An earlier answer invented a table name (`smart_search`; the real one is `asset_job_status`).
* Recommendations named Java libraries (Hystrix, Resilience4j) for a Node.js server and an invented source
  path (`src/modules/jobs/jobs.ts`).

Mitigations now in the code: numbers and fault names in AI text are checked against the evidence
(retry, then fallback); unverified AI recommendations are labelled as such in the report; ranking, scores
and pass/fail never come from the LLM. Statement-level claims still need human review, which is why this
report was written from the data.

## Manual findings (by hand, web UI)
See [MANUAL_FINDINGS.md](MANUAL_FINDINGS.md): M-01 (ML hung → spinner forever), M-02 (ML down → error toast +
misleading "No results"), M-03 (Redis hung → upload stuck at "Processed 0/1", asset stored). All three were
reproduced by the automated run (F3, F4, F2).
