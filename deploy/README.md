# deploy/

Stage coordinator for the image-gen worker, modeled on
`../Application`'s "everything per-stage" convention. The worker is a single
surface (one Docker container), so each stage is a self-contained directory
under `stages/<stage>/` with its own env + compose + entry scripts.

```
deploy/
└── stages/
    ├── dev/               # emulator-backed, no GPU/creds (DESIGN.md §9)
    │   ├── env.sh             # stage config (overridable inline)
    │   ├── docker-compose.yml # fake-gcs + mock-comfyui + worker
    │   ├── up.sh / down.sh    # start (build + --wait) / stop
    │   ├── smoke.py / smoke.sh # seed GCS → publish job → read completion
    │   └── README.md
    ├── preprod/           # real GCP preprod, SA-key secret
    │   ├── env.sh
    │   └── deploy.sh          # build local image + recreate
    └── prod/              # real GCP, pinned image, SA-key secret (DESIGN.md §10)
        ├── env.sh
        ├── docker-compose.yml
        ├── deploy.sh          # pull + recreate (graceful drain)
        └── README.md
```

"One image, two environments" (DESIGN.md §1): the same `Dockerfile` (repo root)
builds the image all stages run — only the env injected per stage differs.

## Usage

```bash
# Dev: emulator stack wired to the Application local stack's Pub/Sub.
deploy/stages/dev/up.sh         # build + start
deploy/stages/dev/smoke.sh      # verify end-to-end
deploy/stages/dev/down.sh       # stop

# Preprod / prod: pull the pinned image and recreate gracefully.
IMAGE=ghcr.io/<org>/imagegen-worker@sha256:<digest> deploy/stages/prod/deploy.sh
```

Each stage's `env.sh` documents its knobs; every value is overridable from the
caller's environment, e.g.:

```bash
COMFYUI_URL=http://host.docker.internal:8188 deploy/stages/dev/up.sh
GCP_PROJECT_ID=tarostory-staging MAX_CONCURRENCY=8 deploy/stages/prod/deploy.sh
```

See `stages/dev/README.md` and `stages/prod/README.md` for the per-stage detail,
including the **contract pin** (`image-gen-contract` is unpublished and pinned to
a raw commit of the local `../ImageGenContract` clone — push and bump that pin
whenever the contract changes; see CLAUDE.md "Contract pin").
