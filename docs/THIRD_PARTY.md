# Third-party software, services and data

| Item | License / terms | How we use it |
|---|---|---|
| [Immich](https://github.com/immich-app/immich) v3.3.1 | AGPL-3.0 | The target application. **Not vendored and not modified**: installed separately from the official release `docker-compose.yml`. ShutterStorm only calls its REST API and controls its containers through the Docker CLI. Immich's compose file and `.env` are never committed here. |
| Immich container images (server, machine-learning, postgres+vectorchord) and Valkey | AGPL-3.0 / respective licenses | Pulled by Immich's own compose file. ShutterStorm never starts extra copies; a fix suggestion may recommend one. |
| [nicolaka/netshoot](https://github.com/nicolaka/netshoot) | Apache-2.0 | Optional helper image for the `ml_latency` / `ml_packet_loss` faults (`tc netem`). Only pulled if you run them. |
| [Mistral AI API](https://docs.mistral.ai/) | Mistral terms of service | Chat completions (`ministral-8b-latest` by default) for the Planner, Analyst, Fix Advisor and Reporter agents. Only sandbox evidence is sent (probe results, trimmed container logs, config subsets); secrets and tokens are redacted. Optional: everything works with `LLM_MODE=off`. |
| [requests](https://github.com/psf/requests) | Apache-2.0 | HTTP client. |
| [Lucide](https://lucide.dev) icons (lucide-static 0.460.0) | ISC | 32 icons vendored into `shutterstorm/web/js/icons.js` for the dashboard; no runtime download. |
| [python:3.12-slim](https://hub.docker.com/_/python), [docker:27.3.1-cli](https://hub.docker.com/_/docker) images | PSF / Apache-2.0 | Base images of our agents and controller containers (`deploy/Dockerfile`). Only pulled when you build the containers. |
| [pytest](https://github.com/pytest-dev/pytest) | MIT | Tests (dev only). |
| COCO128 images (optional, for `seed`) | CC BY 4.0 (COCO) | Optional test content uploaded into **our own** Immich. Not committed (`datasets/` is gitignored). |

Probe images are generated at runtime (random 64×64 PNGs) and deleted after each run.
