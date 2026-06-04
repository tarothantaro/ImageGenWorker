# Source this file (don't execute it).
# Stage: dev — emulator-backed, no GPU, no GCP credentials (DESIGN.md §9).
#
#   . deploy/stages/dev/env.sh
#
# Single source of truth for the dev worker's config. docker-compose.yml reads
# these via ${VAR} substitution; up.sh/down.sh/smoke.sh source this first. Every
# value is overridable from the caller's environment.
#
# Wiring: this stack does NOT run its own Pub/Sub emulator. It joins the
# Application's local stack (../../../../Application/server/deploy/stages/local)
# so the API server and the worker share ONE emulator — the worker pulls the
# jobs the server publishes. That stack must be up first (its up.sh), and uses
# project `tarostory-local`, which is why GCP_PROJECT_ID defaults to it here.

export STAGE="dev"

# Must match the Application local stack's GOOGLE_CLOUD_PROJECT so the worker
# pulls/publishes the same Pub/Sub resources the API server created.
export GCP_PROJECT_ID="${GCP_PROJECT_ID:-tarostory-local}"

# Derived resource paths (same names in every stage — the §6.1 canonical form).
export JOBS_SUBSCRIPTION="${JOBS_SUBSCRIPTION:-projects/${GCP_PROJECT_ID}/subscriptions/image-gen-jobs-worker-sub}"
export COMPLETION_TOPIC="${COMPLETION_TOPIC:-projects/${GCP_PROJECT_ID}/topics/job-completed}"

# Emulator endpoints, reached by docker service name:
#   * pubsub-emulator — lives in the Application local stack; we attach to its
#     docker network (APPSTACK_NETWORK) to resolve the name.
#   * fake-gcs-server / mock-comfyui — brought up by THIS stack's compose.
export PUBSUB_EMULATOR_HOST="${PUBSUB_EMULATOR_HOST:-pubsub-emulator:8085}"
export STORAGE_EMULATOR_HOST="${STORAGE_EMULATOR_HOST:-http://fake-gcs-server:4443}"
# Deterministic local generation: the bundled mock ComfyUI (tests/mock_comfyui)
# the integration test already pins. Point at the real one with
#   COMFYUI_URL=http://host.docker.internal:8188 ./deploy/deploy.sh dev
export COMFYUI_URL="${COMFYUI_URL:-http://mock-comfyui:8188}"

# Worker knobs (DESIGN.md §10.4 dev column).
export MAX_CONCURRENCY="${MAX_CONCURRENCY:-2}"
export MAX_PROCESSING_SECONDS="${MAX_PROCESSING_SECONDS:-60}"
export MODEL_VERSION="${MODEL_VERSION:-comfyui-flux2}"
export LOG_LEVEL="${LOG_LEVEL:-debug}"

# The external docker network the Application local stack created (compose
# project "local" → network "local_default"). The worker attaches to it to
# reach pubsub-emulator by name. Override if your local stack uses another name
# (check: docker network ls).
export APPSTACK_NETWORK="${APPSTACK_NETWORK:-local_default}"

# GCS bucket the smoke test seeds inputs into and the worker writes outputs to.
# Job messages built by smoke.py use gs://$GCS_BUCKET/... URIs.
export GCS_BUCKET="${GCS_BUCKET:-tarostory-local-images}"
