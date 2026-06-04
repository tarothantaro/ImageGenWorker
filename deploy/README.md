# deploy/

Stage coordinator for the image-gen worker, modeled on
`../Application`'s "everything per-stage" convention. The worker is a single
surface (one Docker container), so each stage is a self-contained directory
under `stages/<stage>/` with its own env + compose + entry scripts.

```
deploy/
├── deploy.sh              # thin dispatcher: ./deploy.sh <dev|prod> [args]
└── stages/
    ├── dev/               # emulator-backed, no GPU/creds (DESIGN.md §9)
    │   ├── env.sh             # stage config (overridable inline)
    │   ├── docker-compose.yml # fake-gcs + mock-comfyui + worker
    │   ├── up.sh / down.sh    # start (build + --wait) / stop
    │   ├── smoke.py / smoke.sh # seed GCS → publish job → read completion
    │   └── README.md
    └── prod/              # real GCP, pinned image, SA-key secret (DESIGN.md §10)
        ├── env.sh
        ├── docker-compose.yml
        ├── deploy.sh          # pull + recreate (graceful drain)
        └── README.md
```

"One image, two environments" (DESIGN.md §1): the same `Dockerfile` (repo root)
builds the image both stages run — only the env injected per stage differs.

## Usage

```bash
# Dev: emulator stack wired to the Application local stack's Pub/Sub.
./deploy/deploy.sh dev          # == deploy/stages/dev/up.sh
deploy/stages/dev/smoke.sh      # verify end-to-end

# Prod: pull the pinned image and recreate gracefully.
IMAGE=ghcr.io/<org>/imagegen-worker@sha256:<digest> ./deploy/deploy.sh prod
```

Each stage's `env.sh` documents its knobs; every value is overridable from the
caller's environment, e.g.:

```bash
COMFYUI_URL=http://host.docker.internal:8188 ./deploy/deploy.sh dev
GCP_PROJECT_ID=tarostory-staging MAX_CONCURRENCY=8 ./deploy/deploy.sh prod
```

See `stages/dev/README.md` and `stages/prod/README.md` for the per-stage detail,
including the dev **local-contract bridge** (the worker emits `panel_completed`,
which the pinned `image-gen-contract` ref predates — dev installs the sibling
`../ImageGenContract` clone until that contract is published and the pins are
bumped).
