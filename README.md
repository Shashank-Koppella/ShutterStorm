# ShutterStorm

**AI-driven chaos engineering for a self-hosted [Immich](https://immich.app) photo app.**
Bosch hackathon, problem statement *"AI-Driven Chaos Engineering for Cloud-Native Resilience"*.

ShutterStorm maps Immich's services and dependencies, lets an **AI planner** pick and rank the most
informative failure experiments from that map, injects them into Immich running in **our own Docker
sandbox** (one at a time, or combined), **rolls a fault back automatically when impact crosses a safety
threshold**, measures what users experience (errors, slowdowns, hangs, recovery time, data integrity),
ranks the weaknesses with a fixed formula, lets an LLM (Mistral) explain them, and **suggests fixes**.
It never applies a fix: suggestions are shown for a human to decide on, and `--compare-to` measures the
before/after once a human has applied one.

> Headline finding: **a hung dependency is handled much worse than a down one.** A frozen database,
> Redis or ML service makes requests hang silently (smart search spins forever, uploads stall yet are
> stored as "ghost uploads"); a stopped ML service fails fast. See [docs/FINDINGS_REPORT.md](docs/FINDINGS_REPORT.md).

## Deliverables

| Deliverable | Where |
|---|---|
| 1. Source code + README (setup, how to run) | this repository (MIT); setup below |
| 2. Demo video (3 min) | shot list and narration: [docs/DEMO_VIDEO.md](docs/DEMO_VIDEO.md) |
| 3. Architecture overview (one page) | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| 4. Findings report (ranked weaknesses + fixes) | [docs/FINDINGS_REPORT.md](docs/FINDINGS_REPORT.md) |
| Dependency graphs (generated) | [docs/DEPENDENCY_GRAPH.md](docs/DEPENDENCY_GRAPH.md) |
| Manual findings, API quirks, third-party software | [MANUAL_FINDINGS](docs/MANUAL_FINDINGS.md) · [API_NOTES](docs/API_NOTES.md) · [THIRD_PARTY](docs/THIRD_PARTY.md) |
| Live demo script (5 min, presenting in person) | [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) |

## What one run does

```
Check environment, scope and clean checkpoint
  -> Discover the dependency map (compose + Immich's live config: finds server -> ML, undeclared in compose)
  -> AI experiment planner: rank the fault catalog on the map, select N experiments, one reason each
  -> Baseline: 7 user journeys, 5 rounds (normal error rate, p50/p95 latency, health)
  -> For each planned experiment (riskiest / most informative first):
       predict impact -> inject (journaled, TTL, guard) -> probe for ~35 s
         -> safety monitor after every probe round: error rate / p95 latency / blast radius
            breached in a round -> AUTO-ROLLBACK at once
       -> undo + verify -> measure recovery time and upload integrity -> score (formula)
       -> AI explains -> AI suggests a fix (checked against allowed settings; NOT applied)
  -> Restore and verify cleanup (always, also on error / STOP / Ctrl+C)
  -> report.html: plan, ranked findings, heatmap, resilience score, fixes, safety record, before/after
```

User journeys probed: **J0** ping, **J1** login, **J2** browse (DB), **J3** smart search (ML + DB),
**J4** upload, **J5** thumbnail generated (async, Redis queue), **J6** SHA-1 of the downloaded original.
A request that exceeds the 10 s probe timeout counts as **hung**, distinct from a **fast failure**.

### Failure types (fixed catalog; `python -m shutterstorm list`)

| Failure type (hackathon list) | ShutterStorm faults |
|---|---|
| Service or container crash | `server_restart`, `ml_stop`, `redis_stop` |
| Failed / hung dependency | `ml_pause`, `redis_pause`, `db_pause` |
| Overloaded database / dependency | `db_cpu_throttle`, `ml_cpu_throttle` (Docker CPU limit 0.25) |
| Sudden traffic spike | `traffic_spike` (24 concurrent read-only clients against our Immich) |
| Slow or lossy network | `ml_latency` (+3000 ms), `ml_packet_loss` (30 %) via `tc netem` (optional, needs `nicolaka/netshoot`) |
| Combined failures | `spike_ml_down` (spike + ML down), `spike_redis_hung` (spike + Redis frozen) |

Full disk is deliberately **not** injected: filling a disk on a laptop Docker host is not contained to our
sandbox (Docker Desktop shares one VM disk with everything else).

## Setup and deployment (Windows PowerShell; macOS/Linux: the same with `cp` and `/` paths)

Prerequisites: Docker Desktop (Compose v2), Python 3.10+, ~6 GB RAM free for Immich. A Mistral API key
is optional (`LLM_MODE=off` uses deterministic templates everywhere, including the planner).

### 1. Deploy the sample app (Immich v3.3.1, official Docker Compose files)

```powershell
mkdir $HOME\Desktop\Hackathon; cd $HOME\Desktop\Hackathon
Invoke-WebRequest https://github.com/immich-app/immich/releases/download/v3.3.1/docker-compose.yml -OutFile docker-compose.yml
Invoke-WebRequest https://github.com/immich-app/immich/releases/download/v3.3.1/example.env -OutFile .env
# edit .env: UPLOAD_LOCATION=./library, DB_DATA_LOCATION=./postgres, IMMICH_VERSION=v3.3.1, a random DB_PASSWORD
docker compose up -d          # project "immich": immich_server, immich_machine_learning, immich_redis, immich_postgres
```
Open http://localhost:2283, create the admin account (this is the `IMMICH_EMAIL` / `IMMICH_PASSWORD` below).
Immich is AGPL-3.0 and is not part of this repository; we never modify its files (verified after every run).

### 2. Install and configure ShutterStorm

```powershell
git clone https://github.com/Shashank-Koppella/ShutterStorm.git; cd ShutterStorm
pip install -r requirements.txt -r requirements-dev.txt
Copy-Item shutterstorm.env.example shutterstorm.env    # fill IMMICH_EMAIL, IMMICH_PASSWORD, IMMICH_DIR, MISTRAL_API_KEY
python -m shutterstorm doctor                          # every line should be [OK]
python -m shutterstorm seed --dir <folder of photos> --wait   # optional test content (e.g. COCO128)
```

### 3. Run

```powershell
python -m shutterstorm plan                 # dry run: what the AI planner would test and why (injects nothing)
python -m shutterstorm run                  # full AI-planned series (6 experiments, ~7 min) -> runs/<id>/report.html
python -m shutterstorm dashboard            # web UI at http://127.0.0.1:8765 ("Run AI plan", "Inject fault", Stop, Panic)
```

No Docker? Every command accepts `--simulate` (fake Docker + fake Immich, all output marked SIMULATED):
`python -m shutterstorm --simulate run` and `python -m pytest -q` (67 tests, also in CI).

## Commands (`python -m shutterstorm <command>`)

| Command | What it does |
|---|---|
| `doctor` | checks Docker, containers, health, scope labels, Immich login, Mistral key, disk, journal |
| `discover` | dependency map -> `runs/<id>/topology.json` + `topology.md` |
| `plan [--budget N]` | AI planner dry run: ranked plan with reasons + why the rest was not selected |
| `baseline` | probes only, healthy state |
| `inject F --duration S [--max-error-rate X]` | one fault (or combo) with kill switch, TTL, guard and safety thresholds, live output |
| `run` | full workflow, experiments chosen by the AI planner (`--budget N`, default 6) |
| `run --experiments a,b` / `--no-plan` | operator-chosen faults / the fixed default list instead of the planner |
| `run --compare-to RUN` / `compare --before A --after B` | before/after table (after a human applied a fix) |
| `run ... --max-error-rate X --max-p95-ms Y --breach-rounds N --no-blast-radius` | override the safety thresholds |
| `dashboard [--port 8765]` | local web UI: live containers, AI plan, inject, strict-limit auto-rollback demo, heatmap, fixes, before/after |
| `seed --dir P [--wait]` | upload a folder of images; `--wait` also times thumbnail/ML processing |
| `panic` / `stop` | kill switch: undo everything now / stop a running workflow at its next step |
| `drill` | guard drill: inject ml_pause, hard-kill the owning process, watch the guard restore |
| `report --run ID [--reanalyze]` | rebuild report.md / report.html from saved data |
| `list` | fault catalog and the settings a fix suggestion may use |

`python -m shutterstorm.depgraph` regenerates [docs/DEPENDENCY_GRAPH.md](docs/DEPENDENCY_GRAPH.md).

Each run writes `runs/<timestamp>-run/`: `checkpoint/`, `topology.json`, `plan.json`, `probes.csv`,
`evidence/<fault>/` (probe results, safety-monitor rounds, log tails, queue snapshots, load stats),
`findings.json`, `cleanup.json`, `report.md`, `report.html`, `topology.md`, `run.log`.

## Configuration (`shutterstorm.env`, gitignored; environment variables override it)

| Key | Default | Meaning |
|---|---|---|
| `IMMICH_URL` | `http://localhost:2283` | Immich web/API |
| `IMMICH_EMAIL`, `IMMICH_PASSWORD` | | admin account (never logged) |
| `IMMICH_DIR` | | folder with Immich's `docker-compose.yml` (read only) |
| `MISTRAL_API_KEY` | | optional; redacted from all output |
| `MISTRAL_MODEL` | `ministral-8b-latest` | our free-tier key had no quota for mistral-small (API_NOTES #7) |
| `LLM_MODE` | `live` | `live` / `replay` (cached answers only, for demos) / `off` (templates) |
| `MAX_FAULT_SECONDS` | `60` | hard TTL for any fault |
| `PROBE_TIMEOUT_SECONDS` | `10` | longer = "hung" |
| `ABORT_ERROR_RATE` | `0.75` | roll back when more than this fraction of user calls in a probe round fail |
| `ABORT_P95_MS` | `8000` | roll back when the p95 latency of successful calls in a round is higher |
| `ABORT_BLAST_RADIUS` | `on` | roll back when a journey that does not depend on the faulted service fails |
| `ABORT_ROUNDS` | `1` | consecutive breached rounds before the rollback |

## Safety model

1. **Scope check before every Docker action**: only the four Immich containers (label
   `com.docker.compose.project=immich`) and our own labelled `shutterstorm_*` helpers. The traffic
   generator only talks to `IMMICH_URL` and only sends read-only requests.
2. **Fixed fault catalog**; anything else is rejected, including by the AI planner's validator.
   Postgres is never killed; `db_stop` and the netem faults are never picked automatically.
3. **Write-ahead journal**: the undo recipe is written before a fault is applied and removed only after the
   undo is verified. Combined failures get one entry per part.
4. **Threshold safety monitor**: after every probe round, error rate, p95 latency and blast radius are
   checked; a breach for `ABORT_ROUNDS` rounds rolls the fault back immediately and the report marks the
   experiment **AUTO-STOPPED** (in our real run: `db_pause`, see the findings report).
5. **TTL watchdog** (no fault outlives `MAX_FAULT_SECONDS`) and an **independent dead-man guard** process
   that undoes faults if ShutterStorm crashes (`drill` demonstrates it). The traffic generator is a thread
   of the owning process, so it can never outlive it.
6. **Kill switches**: Ctrl+C, `panic`, `stop` (also as dashboard buttons).
7. **Abort rule**: if liveness fails for more than 30 s after an undo, the run stops and runs panic.
8. **Fixes are never applied.** ShutterStorm does not write Immich's config or compose files; cleanup
   verification proves it (system-config unchanged, every file in IMMICH_DIR unchanged).
9. **Cleanup verification** after every run (in the report): containers healthy and unpaused, CPU limits
   restored, no helpers, probe assets deleted (only ours), journal empty.
10. **Where AI is (not) used**: the LLM plans (from the catalog only), predicts, explains, suggests and
   writes prose. It never runs commands, never sets safety limits, never ranks findings (a formula does),
   and its text is checked against the evidence for invented numbers.

## Stretch goals

| Stretch goal | Status |
|---|---|
| Live dashboard / simple UI | `dashboard`: live containers, AI plan, live log, heatmap, auto-stops, fixes, before/after |
| Combined failures | `spike_ml_down`, `spike_redis_hung`; the planner may pick one per run |
| Re-run after applying a fix | `run --experiments <faults> --compare-to <run>`: a human applies the suggested fix, ShutterStorm re-runs and reports the score / heatmap delta per fault |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `doctor`: container not healthy | wait; after a pause Docker may show `unhealthy` for up to 5 min (Postgres checks only every 300 s) |
| `doctor`: STOP file | left by the dashboard Stop button; the next `run`/`inject` removes it |
| `journal has pending undo entries` | `python -m shutterstorm panic` |
| J3 never fails during `ml_pause` | Immich caches query embeddings; probes use unique queries (API_NOTES #1) |
| Mistral 429 on every call | check header `x-ratelimit-limit-req-minute`; `0` = no quota for that model |
| Dashboard port busy | `python -m shutterstorm dashboard --port 8766` |

## License

MIT (see [LICENSE](LICENSE)). Immich is AGPL-3.0 and is **not** included; we only call its API and Docker.
