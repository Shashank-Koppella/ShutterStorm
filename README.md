# ShutterStorm

**AI-driven chaos engineering for a self-hosted [Immich](https://immich.app) photo app.**
Bosch hackathon, problem statement *"AI-Driven Chaos Engineering for Cloud-Native Resilience"*.

ShutterStorm injects controlled failures into Immich running in **our own Docker sandbox**. It measures
what users experience (errors, slowdowns, hangs, recovery time, data integrity), then uses an LLM
(Mistral) to explain the weaknesses and propose fixes. A deterministic scoring formula ranks them and
gives a resilience score. A repair loop tries config-level fixes, re-injects the **same** fault to verify
them, and reverts everything afterwards.

> Headline finding: **a hung dependency is handled much worse than a down one.** A frozen ML service or
> Redis makes requests hang silently (smart search spins forever, uploads stall at "Processed 0/1" yet are
> stored as "ghost uploads"). A stopped one fails fast. See [docs/FINDINGS_REPORT.md](docs/FINDINGS_REPORT.md).

| | |
|---|---|
| Architecture (one page) | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Findings report | [docs/FINDINGS_REPORT.md](docs/FINDINGS_REPORT.md) |
| Manual findings M-01..M-03 | [docs/MANUAL_FINDINGS.md](docs/MANUAL_FINDINGS.md) |
| Demo video script | [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) |
| API quirks we found | [docs/API_NOTES.md](docs/API_NOTES.md) |
| Third-party software | [docs/THIRD_PARTY.md](docs/THIRD_PARTY.md) |

## What it does

```
Check environment, scope and clean checkpoint
  -> Baseline tests (7 user journeys)
  -> Predict impact (dependency map + AI) -> Inject approved fault -> Confirm failure and capture evidence
  -> AI analysis -> [--repair] Agent repairs within allowed scope
  -> Check changed files and run protected tests, re-inject the SAME fault
       |- Fail, retry available -> back to repair (results fed back)
       |- Pass                  -> Save verified patch and report
       '- Attempts exhausted    -> Save unresolved result (still a valid finding)
  -> Restore test environment and verify cleanup   (always: also on error, STOP, Ctrl+C)
```

User journeys probed: **J0** ping, **J1** login, **J2** browse (DB), **J3** smart search (ML + DB),
**J4** upload, **J5** thumbnail generated (async, via Redis queue), **J6** SHA-1 of the downloaded original.
A request that exceeds the probe timeout counts as **hung**, which is kept separate from a **fast failure**.

## Quickstart

### Prerequisites
* Docker Desktop (Windows: WSL2 backend) with **Immich v3.3.1** already running via Docker Compose
  (project name `immich`, containers `immich_server`, `immich_machine_learning`, `immich_redis`,
  `immich_postgres`). Install Immich from the
  [official release compose file](https://github.com/immich-app/immich/releases/download/v3.3.1/docker-compose.yml);
  it is not part of this repo. Use `DB_DATA_LOCATION=pgdata` (a named volume) on Windows.
* An Immich admin account.
* Python 3.10+.
* Optional: a Mistral API key (free tier works). Without it, deterministic fallbacks are used.

### Windows (PowerShell)
```powershell
git clone <this repo> ShutterStorm; cd ShutterStorm
py -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
Copy-Item shutterstorm.env.example shutterstorm.env
notepad shutterstorm.env          # IMMICH_EMAIL, IMMICH_PASSWORD, IMMICH_DIR, MISTRAL_API_KEY
python -m shutterstorm doctor
python -m shutterstorm run --experiments ml_pause,redis_pause,ml_stop --repair
```

### macOS / Linux
```bash
git clone <this repo> ShutterStorm && cd ShutterStorm
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp shutterstorm.env.example shutterstorm.env && $EDITOR shutterstorm.env
python -m shutterstorm doctor
python -m shutterstorm run --experiments ml_pause,redis_pause,ml_stop --repair
```

### No Docker? Use the simulator
Every command accepts `--simulate`. It runs against a fake Docker backend and an in-process fake Immich
that reproduce M-01..M-03. All output is clearly marked **SIMULATED** and is never a real finding.
```bash
python -m shutterstorm --simulate run --repair
python -m pytest -q            # 53 tests, all in simulation
```

## Configuration (`shutterstorm.env`, gitignored; environment variables override it)

| Key | Default | Meaning |
|---|---|---|
| `IMMICH_URL` | `http://localhost:2283` | Immich web/API |
| `IMMICH_EMAIL`, `IMMICH_PASSWORD` | | admin account (never logged) |
| `IMMICH_DIR` | | folder with Immich's `docker-compose.yml` and `.env` |
| `MISTRAL_API_KEY` | | optional; never printed or sent anywhere except api.mistral.ai |
| `MISTRAL_MODEL` | `ministral-8b-latest` | the free tier had no quota for mistral-small (API_NOTES #7) |
| `LLM_MODE` | `live` | `live` / `replay` (cache only, demo safety net) / `off` (deterministic fallbacks) |
| `MAX_FAULT_SECONDS` | `60` | hard TTL for any fault |
| `PROBE_TIMEOUT_SECONDS` | `10` | longer = "hung" |
| `MAX_REPAIR_ATTEMPTS` | `3` | |
| `LLM_BUDGET` | `40` | max API calls per run |

## Commands (`python -m shutterstorm <command>`)

| Command | What it does |
|---|---|
| `doctor` | checks Docker, containers, health, scope labels, Immich login, Mistral key, disk, journal |
| `discover` | dependency map -> `runs/<id>/topology.json` + `topology.md` (Mermaid); edges labelled compose / config / empirical |
| `seed --dir P` | upload a folder of images (e.g. COCO128) as test content; duplicates are skipped |
| `baseline` | probes only, healthy state |
| `inject F --duration S` | one fault with kill switch + TTL + guard (the Day-1 deliverable) |
| `run [--experiments a,b] [--repair] [--keep-fix]` | full workflow; default set: ml_pause, ml_stop, redis_pause, redis_stop, db_pause, server_restart |
| `panic` | kill switch: undo everything, restore the last checkpoint |
| `stop` | create the STOP file; a running workflow stops at its next step |
| `drill` | guard drill: inject ml_pause, hard-kill the owning process, watch the guard restore |
| `report --run ID` | regenerate report.md / report.html from saved data |
| `list` | fault catalog and allowed repair knobs |

Every run writes `runs/<timestamp>-run/`: `checkpoint/`, `probes.csv`, `evidence/<exp>/` (probe results,
container log tails, queue snapshots), `findings.json`, `patches/`, `cleanup.json`, `report.md`,
`report.html` (single self-contained page), `topology.md`, `run.log`.

## Safety model

ShutterStorm only ever touches **our own sandbox**:

1. **Scope check before every Docker action.** The target must be one of the four Immich containers (or our
   own `immich_machine_learning_2` / labelled `shutterstorm_*` helpers) **and** carry
   `com.docker.compose.project=immich`. Anything else is refused.
2. **Fault catalog.** Only the 9 faults in `python -m shutterstorm list`; anything else is rejected. Postgres
   is only paused or stopped gracefully, never killed.
3. **Write-ahead journal** (`.shutterstorm/journal.json`): the undo recipe with the original values is written
   *before* any change; an entry is removed only after the undo is *verified*.
4. **TTL watchdog**: no fault outlives `MAX_FAULT_SECONDS`.
5. **Independent dead-man guard**: a detached process undoes journal entries whose owner died or whose TTL
   passed, even if ShutterStorm crashed (`python -m shutterstorm drill` demonstrates this).
6. **Kill switches**: Ctrl+C, `python -m shutterstorm panic`, and the `.shutterstorm/STOP` file (checked between
   every step).
7. **Abort rule**: if liveness (J0) fails for more than 30 s after an undo, the run stops and runs panic.
8. **Fail-closed repair validator**: only the knobs in `list` are accepted (e.g. ML URLs from a fixed list, job
   concurrency 1-8, a second ML instance with a fixed shape). We never edit `docker-compose.yml` or `.env`; the
   only file we may write is `docker-compose.shutterstorm.yml`, and a hash of every file in `IMMICH_DIR` proves it.
9. **Cleanup verification** after every run (shown in the report): containers healthy and unpaused, CPU limits,
   no helpers, no netem, system-config identical to the checkpoint, override file restored, probe assets
   deleted (only ours: by ID **and** `shutterstorm-probe-` file name), journal empty.
10. **Where AI is (not) used**: the LLM predicts, explains, proposes patches and writes prose. It never runs
    commands, never decides whether a patch passed, and never ranks findings. Only sandbox evidence is sent;
    secrets and tokens are redacted.

## Reproduce our results

```powershell
python -m shutterstorm doctor
python -m shutterstorm discover                       # shows the undeclared server -> ML edge
python -m shutterstorm inject ml_pause --duration 20  # M-01: J3 hangs, auto-restore
python -m shutterstorm drill                          # guard restores after a hard kill
python -m shutterstorm run --experiments ml_pause,ml_stop,redis_pause,redis_stop,db_pause,server_restart --repair
```
A full default run takes about 10-15 minutes. Our results are in [docs/FINDINGS_REPORT.md](docs/FINDINGS_REPORT.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `doctor`: container not healthy | `docker compose ps` in IMMICH_DIR; wait for health; after a pause Docker may show `unhealthy` for up to 30 s |
| `journal has pending undo entries` | `python -m shutterstorm panic` |
| A run stops immediately with "STOP file present" | an old STOP file is cleared when `run`/`inject` start; if you created one manually, delete `.shutterstorm/STOP` |
| J3 never fails during `ml_pause` | Immich caches query embeddings; probes use unique queries (see docs/API_NOTES.md #1) |
| Mistral 429 on every call | check the `x-ratelimit-limit-req-minute` response header: if it is `0`, your key has no quota for that model. We use `MISTRAL_MODEL=ministral-8b-latest` (see docs/API_NOTES.md #7). Occasional 429s are backed off automatically; use `LLM_MODE=replay` for demos |
| `ml_latency` refuses to run | `docker pull nicolaka/netshoot` first (optional fault) |
| Second ML instance does not start | it needs ~1-2 GB extra RAM in Docker Desktop's WSL VM |

## License

ShutterStorm is MIT-licensed (see [LICENSE](LICENSE)). Immich is AGPL-3.0 and is **not** included: we
only call its API and Docker. See [docs/THIRD_PARTY.md](docs/THIRD_PARTY.md).
