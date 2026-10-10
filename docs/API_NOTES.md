# Immich API notes (observed on the live v3.3.1 server)

Things that behaved differently from what the OpenAPI spec alone suggests, and how
ShutterStorm handles them.

| # | Observation | Consequence | Handling in ShutterStorm |
|---|-------------|-------------|--------------------------|
| 1 | `POST /api/search/smart` with a query text that was already searched answers in ~70 ms **even while the ML container is paused**. The server caches the CLIP text embedding per query string. | A probe that always searches `"tree"` never reaches ML and hides the M-01 hang. Our first `inject ml_pause` run showed J3 36/36 OK for this reason. | J3 sends a unique query per call (`tree <run> <n>`). The warmup also uses a unique query. |
| 2 | `POST /api/search/metadata` accepts `originalFileName` as an exact filter. | An upload whose HTTP response never arrived (timeout) can still be found afterwards. | `AssetTracker.reconcile()` finds these "ghost uploads" so cleanup deletes them and the integrity score counts them. |
| 3 | `machineLearning.availabilityChecks` is **already enabled** by default (`timeout 2000`, `interval 30000`) with a single ML URL. | The availability check alone does not prevent the M-01 hang: with only one URL there is nowhere to fail over to. | The Repair agent adds a second ML URL/instance instead of only toggling the check. |
| 4 | `docker update --cpus 0 <container>` is accepted by Docker 29.8 and resets `NanoCpus` to 0 (unlimited). | No container recreation needed to undo `ml_cpu_throttle`. | `_restore_cpus` still falls back to `docker compose up -d --force-recreate <service>` if a Docker version rejects it. |
| 5 | `DELETE /api/assets` with `{"ids": [...], "force": true}` deletes permanently (skips trash) and answers 204. | | Only IDs we uploaded **and** whose `originalFileName` starts with `shutterstorm-probe-` are deleted. |
