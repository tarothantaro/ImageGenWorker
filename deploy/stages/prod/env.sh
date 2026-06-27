# Source this file (don't execute it).
# Stage: prod — real GCP, the same image as dev (DESIGN.md §1, §10).
#
#   . deploy/stages/prod/env.sh
#
# Config injected into the worker on a GPU-less worker host (the GPU lives in
# the separate ComfyUI container the worker talks to over HTTP). The SA key is
# mounted as a Docker secret by docker-compose.yml — never baked into the image
# (DESIGN.md §10.3). Every value is overridable from the caller's environment.

export STAGE="prod"

export GCP_PROJECT_ID="${GCP_PROJECT_ID:-tarostory-prod}"
export JOBS_SUBSCRIPTION="${JOBS_SUBSCRIPTION:-projects/${GCP_PROJECT_ID}/subscriptions/image-gen-jobs-worker-sub}"
export COMPLETION_TOPIC="${COMPLETION_TOPIC:-projects/${GCP_PROJECT_ID}/topics/job-completed}"

# Single bucket the worker reads inputs from and writes outputs to. The job
# message carries no gcs_uri/output_prefix (DESIGN.md §5.1) — the worker derives
# both paths from this bucket + the message ids (config.py WorkerConfig.gcs_bucket,
# a required var). Must match the bucket the API server uploads inputs to.
export GCS_BUCKET="${GCS_BUCKET:-tarostory-prod-images}"

# Worker knobs (DESIGN.md §10.4 prod column). MAX_PROCESSING_SECONDS is the
# total lease extension window for one full story job; the client library renews
# the lease in smaller increments under Pub/Sub's per-extension 600s ceiling.
export MAX_CONCURRENCY="${MAX_CONCURRENCY:-4}"
export MAX_PROCESSING_SECONDS="${MAX_PROCESSING_SECONDS:-3600}"
export MODEL_VERSION="${MODEL_VERSION:-comfyui-flux2}"
export LOG_LEVEL="${LOG_LEVEL:-info}"

# The worker reaches a host-run ComfyUI via host.docker.internal (compose adds
# the host-gateway mapping). DESIGN.md §10.4.
export COMFYUI_URL="${COMFYUI_URL:-http://host.docker.internal:8188}"

# Pinned image — prod pulls by digest, never :latest (DESIGN.md §7.1). Override
# IMAGE with the cosign-verified digest you deploy.
export IMAGE="${IMAGE:-ghcr.io/tarothantaro/imagegen-worker:latest}"

# Host path to the worker SA key (root:root 0400). Mounted as a Docker secret
# (DESIGN.md §10.3). Override per host if you keep it elsewhere.
export SA_KEY_FILE="${SA_KEY_FILE:-/etc/imagegen/sa.json}"
