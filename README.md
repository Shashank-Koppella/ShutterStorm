# ShutterStorm

**AI-driven chaos engineering for a self-hosted [Immich](https://immich.app) photo app.**
Bosch hackathon, problem statement *"AI-Driven Chaos Engineering for Cloud-Native Resilience"*.

ShutterStorm injects one controlled failure at a time into Immich running in **our own Docker sandbox**,
measures what users experience (errors, slowdowns, hangs, recovery time, data integrity), scores it with a
fixed formula, lets an LLM (Mistral) explain what happened, and **suggests a fix**. It never applies a fix:
suggestions are shown in the terminal, the dashboard and the report for a human to decide on.

> Headline finding: **a hung dependency is handled much worse than a down one.** A frozen ML service or
> Redis makes requests hang silently (smart search spins forever, uploads stall yet are stored as "ghost
> uploads"); a stopped ML service fails fast. See [docs/FINDINGS_REPORT.md](docs/FINDINGS_REPORT.md).

| | |
|---|---|
| Architecture (one page) | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Dependency graphs (generated) | [docs/DEPENDENCY_GRAPH.md](docs/DEPENDENCY_GRAPH.md) |
| Findings report | [docs/FINDINGS_REPORT.md](docs/FINDINGS_REPORT.md) |
| Manual findings M-01..M-03 | [docs/MANUAL_FINDINGS.md](docs/MANUAL_FINDINGS.md) |
| Demo script | [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) |
| API quirks we found | [docs/API_NOTES.md](docs/API_NOTES.md) |
| Third-party software | [docs/THIRD_PARTY.md](docs/THIRD_PARTY.md) |

## What one run does

```
Check environment, scope and clean checkpoint
  -> Discover the dependency map (compose + Immich config)
  -> Baseline: 7 user journeys, 5 rounds
  -> For each fault:
       predict impact -> inject (journaled, TTL, guard) -> probe for ~35 s -> undo + verify
       -> measure recovery and upload integrity -> score (formula) -> AI explains
       -> AI suggests a fix (checked against allowed settings; NOT applied, NOT tested)
  -> Restore and verify cleanup (always, also on error / STOP / Ctrl+C) -> report.html
```

User journeys probed: **J0** ping, **J1** login, **J2** browse (DB), **J3** smart search (ML + DB),
**J4** upload, **J5** thumbnail generated (async, Redis queue), **J6** SHA-1 of the downloaded original.
A request that exceeds the 10 s probe timeout counts as **hung**, distinct from a **fast failure**.

## Quickstart (Windows PowerShell)

Prerequisites: Docker Desktop with **Immich v3.3.1** running via Docker Compose (project `immich`;
containers `immich_server`, `immich_machine_learning`, `immich_redis`, `immich_postgres`), an Immich admin
account, Python 3.10+. A Mistral key is optional (`LLM_MODE=off` uses templates).

```powershell
cd ShutterStorm
pip install -r requirements.txt -r requirements-dev.txt
Copy-Item shutterstorm.env.example shutterstorm.env    # fill IMMICH_EMAIL, IMMICH_PASSWORD, IMMICH_DIR, MISTRAL_API_KEY
python -m shutterstorm doctor                         # every line should be [OK]
python -m shutterstorm dashboard                      # open http://127.0.0.1:8765
```
macOS/Linux: the same commands with `cp` instead of `Copy-Item`.

No Docker? Every command accepts `--simulate` (fake Docker + fake Immich, all output marked SIMULATED):
`python -m shutterstorm --simulate run` and `python -m pytest -q`.

## Commands (`python -m shutterstorm <command>`)

| Command | What it does |
|---|---|
| `doctor` | checks Docker, containers, health, scope labels, Immich login, Mistral key, disk, journal |
| `dashboard [--port 8765]` | **local web UI**: live container states, "Inject fault" button, live log, heatmap, AI analysis, suggested fix, links to reports and the dependency graph |
| `discover` | dependency map -> `runs/<id>/topology.json` + `topology.md` |
| `baseline` | probes only, healthy state |
| `inject F --duration S` | one fault with kill switch + TTL + guard, live probe output |
| `run [--experiments a,b] [--no-suggestions]` | full workflow; default faults: ml_pause, ml_stop, redis_pause, redis_stop, db_pause, server_restart |
| `seed --dir P [--wait]` | upload a folder of images; `--wait` also times thumbnail/ML processing |
| `panic` | kill switch: undo everything, restore the last checkpoint |
| `stop` | STOP file: a running workflow stops safely at its next step |
| `drill` | guard drill: inject ml_pause, hard-kill the owning process, watch the guard restore |
| `report --run ID [--reanalyze]` | rebuild report.md / report.html from saved data |
| `list` | fault catalog and the settings a fix suggestion may use |

`python -m shutterstorm.depgraph` regenerates [docs/DEPENDENCY_GRAPH.md](docs/DEPENDENCY_GRAPH.md).

Each run writes `runs/<timestamp>-run/`: `checkpoint/`, `probes.csv`, `evidence/<fault>/` (probe results, log
tails, queue snapshots), `findings.json`, `cleanup.json`, `report.md`, `report.html`, `topology.md`, `run.log`.

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

## Safety model

1. **Scope check before every Docker action**: only the four Immich containers (label
   `com.docker.compose.project=immich`) and our own labelled `shutterstorm_*` helpers.
2. **Fixed fault catalog** (`list`); anything else is rejected. Postgres is never killed.
3. **Write-ahead journal**: the undo recipe is written before a fault is applied and removed only after the
   undo is verified.
4. **TTL watchdog** (no fault outlives `MAX_FAULT_SECONDS`) and an **independent dead-man guard** process
   that undoes faults if ShutterStorm crashes (`drill` demonstrates it).
5. **Kill switches**: Ctrl+C, `panic`, `stop` (also as dashboard buttons).
6. **Abort rule**: if liveness fails for more than 30 s after an undo, the run stops and runs panic.
7. **Fixes are never applied.** ShutterStorm does not write Immich's config or compose files; cleanup
   verification proves it (system-config unchanged, every file in IMMICH_DIR unchanged).
8. **Cleanup verification** after every run (in the report): containers healthy and unpaused, CPU limits
   restored, no helpers, probe assets deleted (only ours), journal empty.
9. **Where AI is (not) used**: the LLM predicts, explains, suggests and writes prose. It never runs commands,
   never ranks findings (a formula does) and its text is checked against the evidence for invented numbers.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `doctor`: container not healthy | wait; after a pause Docker may show `unhealthy` for up to 5 min (Postgres checks only every 300 s) |
| `journal has pending undo entries` | `python -m shutterstorm panic` |
| J3 never fails during `ml_pause` | Immich caches query embeddings; probes use unique queries (API_NOTES #1) |
| Mistral 429 on every call | check header `x-ratelimit-limit-req-minute`; `0` = no quota for that model |
| Dashboard port busy | `python -m shutterstorm dashboard --port 8766` |

## License

MIT (see [LICENSE](LICENSE)). Immich is AGPL-3.0 and is **not** included; we only call its API and Docker.
