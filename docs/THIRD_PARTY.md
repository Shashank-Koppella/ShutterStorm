# Third-party software, services and data

| Item | License / terms | How we use it |
|---|---|---|
| [Immich](https://github.com/immich-app/immich) v3.3.1 | AGPL-3.0 | The target application. **Not vendored and not modified**: installed separately from the official release `docker-compose.yml`. ShutterStorm only calls its REST API and controls its containers through the Docker CLI. Immich's compose file and `.env` are never committed here. |
| Immich container images (server, machine-learning, postgres+vectorchord) and Valkey | AGPL-3.0 / respective licenses | Pulled by Immich's own compose file. The repair loop may start a second copy of the same ML image. |
| [nicolaka/netshoot](https://github.com/nicolaka/netshoot) | Apache-2.0 | Optional helper image for the `ml_latency` fault (`tc netem`). Only pulled if you run that fault. |
| [Mistral AI API](https://docs.mistral.ai/) | Mistral terms of service | Chat completions (`ministral-8b-latest` by default) for the Analyst, Repair and Reporter agents. Only sandbox evidence is sent (probe results, trimmed container logs, config subsets); secrets and tokens are redacted. Optional: everything works with `LLM_MODE=off`. |
| [requests](https://github.com/psf/requests) | Apache-2.0 | HTTP client. |
| [pytest](https://github.com/pytest-dev/pytest) | MIT | Tests (dev only). |
| COCO128 images (optional, for `seed`) | CC BY 4.0 (COCO) | Optional test content uploaded into **our own** Immich. Not committed (`datasets/` is gitignored). |

Probe images are generated at runtime (random 64×64 PNGs) and deleted after each run.
