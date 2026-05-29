# Source this file (don't execute it). STAGE must be set before sourcing.
#
# Stage-aware environment for the image-gen worker, consumed by deploy.sh and
# the docker-compose files. Mirrors ../Application/client/deploy/env.sh: one
# place for everything that varies by stage, so the compose files stay generic
# and the operator picks an environment with a single STAGE.
#
# "One image, two environments" (DESIGN.md §1): the same container runs in dev
# (against emulators) and prod (against GCP). Only what's exported here differs.

: "${STAGE:?STAGE must be set (dev|prod) before sourcing deploy/env.sh}"

case "$STAGE" in
  dev)
    # Emulators replace Pub/Sub + GCS; no GCP credentials needed (DESIGN §9).
    export GCP_PROJECT_ID="${GCP_PROJECT_ID:-dev-project}"
    export MAX_CONCURRENCY="${MAX_CONCURRENCY:-2}"
    export MAX_PROCESSING_SECONDS="${MAX_PROCESSING_SECONDS:-60}"
    export LOG_LEVEL="${LOG_LEVEL:-debug}"
    export PUBSUB_EMULATOR_HOST="${PUBSUB_EMULATOR_HOST:-pubsub-emulator:8085}"
    export STORAGE_EMULATOR_HOST="${STORAGE_EMULATOR_HOST:-http://fake-gcs-server:4443}"
    export COMPOSE_FILE="docker-compose.dev.yml"
    ;;
  prod)
    # Real GCP; SA key is injected as a Docker secret by the compose file (§10).
    export GCP_PROJECT_ID="${GCP_PROJECT_ID:-tarostory-prod}"
    export MAX_CONCURRENCY="${MAX_CONCURRENCY:-4}"
    export MAX_PROCESSING_SECONDS="${MAX_PROCESSING_SECONDS:-540}"
    export LOG_LEVEL="${LOG_LEVEL:-info}"
    export COMPOSE_FILE="docker-compose.yml"
    ;;
  *)
    echo "[deploy/env] unknown STAGE=$STAGE (expected dev|prod)" >&2
    return 1 2>/dev/null || exit 1
    ;;
esac

# Derived from GCP_PROJECT_ID — same names in both stages (the emulator honours
# the projects/<id>/... canonical form too).
export JOBS_SUBSCRIPTION="${JOBS_SUBSCRIPTION:-projects/${GCP_PROJECT_ID}/subscriptions/image-gen-jobs-worker-sub}"
export COMPLETION_TOPIC="${COMPLETION_TOPIC:-projects/${GCP_PROJECT_ID}/topics/job-completed}"

# Where the model reaches ComfyUI, and the model id stamped onto completions.
# On a Linux Docker host the container reaches a host-run ComfyUI via
# host.docker.internal (add --add-host=host.docker.internal:host-gateway).
export COMFYUI_URL="${COMFYUI_URL:-http://host.docker.internal:8188}"
export MODEL_VERSION="${MODEL_VERSION:-comfyui-flux2}"
