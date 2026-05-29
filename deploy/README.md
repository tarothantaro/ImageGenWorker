# deploy/

Stage coordinator for the image-gen worker, modeled on
`../Application/client/deploy/`. The worker is a single surface (one Docker
container), so unlike the client coordinator this dispatches to one compose
stack per stage rather than to multiple per-surface scripts.

Two files do the work:

1. **`env.sh`** — STAGE-aware environment, sourced first by `deploy.sh`.
   Everything that varies by stage but is shared by the worker and the compose
   files lives here: `GCP_PROJECT_ID`, `JOBS_SUBSCRIPTION`, `COMPLETION_TOPIC`,
   `MAX_CONCURRENCY`, `MAX_PROCESSING_SECONDS`, `LOG_LEVEL`, `COMFYUI_URL`,
   `MODEL_VERSION`, the emulator hosts (dev only), and which `COMPOSE_FILE` to
   use. Every value is overridable from the caller's environment.
2. **`deploy.sh`** — one entry point that sources `env.sh` and brings up the
   right compose stack for the stage.

## Stages

| Stage  | Compose file              | Transport            | Notes |
|--------|---------------------------|----------------------|-------|
| `dev`  | `docker-compose.dev.yml`  | pubsub + GCS emulators | No GCP creds. Foreground `up --build`; Ctrl-C tears down. (DESIGN.md §9) |
| `prod` | `docker-compose.yml`      | real GCP             | SA key via Docker secret. `pull` then detached recreate so the old container drains. (DESIGN.md §10) |

"One image, two environments" (DESIGN.md §1): the same container image runs in
both stages — only the env injected here differs.

## Usage

```bash
# Dev: emulator stack, logs in the foreground.
./deploy/deploy.sh dev

# Prod: pull the pinned image and recreate gracefully.
./deploy/deploy.sh prod

# Extra args pass straight through to docker compose:
./deploy/deploy.sh dev --no-build
./deploy/deploy.sh prod --remove-orphans
```

Override any stage default inline:

```bash
GCP_PROJECT_ID=tarostory-staging MAX_CONCURRENCY=8 ./deploy/deploy.sh prod
```

## Compose files

`deploy.sh` expects `docker-compose.dev.yml` and `docker-compose.yml` at the
repo root. Their specifications (services, GPU reservation, secrets, hardening)
live in **DESIGN.md §9 (dev)** and **§10 (prod)**. If the file is missing,
`deploy.sh` fails fast and points you there rather than running a half-wired
stack.
