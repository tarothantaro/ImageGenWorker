# Source this file (don't execute it).
# Stage: preprod — real GCP on tarostory-preprod, worker on a dev host.
#
#   . deploy/stages/preprod/env.sh
#
# Same shape as prod (deploy/stages/prod/env.sh) with three differences:
#   * GCP project is tarostory-preprod (the pre-production gate project);
#   * IMAGE defaults to a locally-built tag (`docker build -t
#     imagegen-worker:preprod-<sha> .`) — preprod validates the code that
#     is ABOUT to be pinned, so there is no registry pull;
#   * the SA key lives under the operator's ~/.config (no root /etc path
#     on a dev host).
# The ComfyUI backend is the host's real container (ImageGenComfyui repo)
# at host.docker.internal:8188, exactly like prod.

export STAGE="preprod"

export GCP_PROJECT_ID="${GCP_PROJECT_ID:-tarostory-preprod}"
export JOBS_SUBSCRIPTION="${JOBS_SUBSCRIPTION:-projects/${GCP_PROJECT_ID}/subscriptions/image-gen-jobs-worker-sub}"
export COMPLETION_TOPIC="${COMPLETION_TOPIC:-projects/${GCP_PROJECT_ID}/topics/job-completed}"

# Worker knobs (DESIGN.md §10.4). MAX_PROCESSING_SECONDS must stay under
# the 600s Pub/Sub ack deadline.
export MAX_CONCURRENCY="${MAX_CONCURRENCY:-2}"
export MAX_PROCESSING_SECONDS="${MAX_PROCESSING_SECONDS:-540}"
export MODEL_VERSION="${MODEL_VERSION:-comfyui-flux2}"
export LOG_LEVEL="${LOG_LEVEL:-info}"

export COMFYUI_URL="${COMFYUI_URL:-http://host.docker.internal:8188}"

_GIT_SHA="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --short HEAD 2>/dev/null || echo dev)"
export IMAGE="${IMAGE:-imagegen-worker:preprod-${_GIT_SHA}}"

export SA_KEY_FILE="${SA_KEY_FILE:-$HOME/.config/tarostory/secrets/imagegen-worker-preprod-sa.json}"
