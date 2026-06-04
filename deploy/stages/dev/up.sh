#!/usr/bin/env bash
# Bring up the dev worker stack (fake-gcs + mock-comfyui + worker), wired to the
# Application local stack's Pub/Sub emulator. Detached + --wait so a follow-up
# `smoke.sh` runs against a healthy stack. Idempotent.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
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
echo "[dev/up] pubsub=$PUBSUB_EMULATOR_HOST gcs=$STORAGE_EMULATOR_HOST comfyui=$COMFYUI_URL" >&2
echo "[dev/up] appstack network=$APPSTACK_NETWORK" >&2

docker compose -f "$HERE/docker-compose.yml" up -d --build --wait "$@"
echo "[dev/up] stack is up. Tail the worker:" >&2
echo "[dev/up]   docker compose -f $HERE/docker-compose.yml logs -f imagegen-worker" >&2
echo "[dev/up] Verify end-to-end (seed GCS → publish job → read completion):" >&2
echo "[dev/up]   $HERE/smoke.sh" >&2
