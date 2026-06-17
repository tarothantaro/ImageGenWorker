#!/usr/bin/env bash
# Bring up the dev worker stack (worker + the Application local stack's Pub/Sub
# emulator). Detached + --wait so a follow-up `smoke.sh` runs against a healthy
# stack. Idempotent.
#
# Backend (overrides env.sh's default of the host's real ComfyUI on :8188):
#   --mock   run the bundled mock ComfyUI (tests/mock_comfyui) — no GPU/models.
#            Equivalent to COMFYUI_BACKEND=mock ./up.sh.
#   --real   force the real backend (the default). Equivalent to
#            COMFYUI_BACKEND=real ./up.sh.
# Any other args pass through to `docker compose up`.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

# Parse backend flags before sourcing env.sh (which reads COMFYUI_BACKEND).
rest=()
for arg in "$@"; do
  case "$arg" in
    --mock) export COMFYUI_BACKEND=mock ;;
    --real) export COMFYUI_BACKEND=real ;;
    *) rest+=("$arg") ;;
  esac
done
set -- "${rest[@]+"${rest[@]}"}"

. "$HERE/env.sh"

# The worker attaches to the Application local stack's network to reach
# pubsub-emulator. Fail fast with a clear pointer if that stack isn't up.
if ! docker network inspect "$APPSTACK_NETWORK" >/dev/null 2>&1; then
  echo "[dev/up] network '$APPSTACK_NETWORK' not found — the Application local stack isn't up." >&2
  echo "[dev/up] start it first:" >&2
  echo "[dev/up]   ../../../../Application/server/deploy/stages/local/up.sh" >&2
  echo "[dev/up] (or set APPSTACK_NETWORK=<name> if your local stack uses another)." >&2
  exit 1
fi

echo "[dev/up] STAGE=$STAGE GCP_PROJECT_ID=$GCP_PROJECT_ID" >&2
echo "[dev/up] backend=$COMFYUI_BACKEND pubsub=$PUBSUB_EMULATOR_HOST gcs=$STORAGE_EMULATOR_HOST comfyui=$COMFYUI_URL" >&2
echo "[dev/up] appstack network=$APPSTACK_NETWORK" >&2

docker compose -f "$HERE/docker-compose.yml" up -d --build --wait "$@"
echo "[dev/up] stack is up. Tail the worker:" >&2
echo "[dev/up]   docker compose -f $HERE/docker-compose.yml logs -f imagegen-worker" >&2
echo "[dev/up] Verify end-to-end (seed GCS → publish job → read completion):" >&2
echo "[dev/up]   $HERE/smoke.sh" >&2
