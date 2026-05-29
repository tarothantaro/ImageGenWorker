#!/usr/bin/env bash
# Single entry point that brings up the image-gen worker for a given stage.
# Mirrors ../Application/client/deploy/deploy.sh: stage-aware env (env.sh) is
# sourced first, then this dispatches to docker compose with the right file.
#
# Usage:
#   ./deploy.sh <stage> [extra docker compose args...]
#     stage : dev | prod
#
#   dev  → emulator stack (docker-compose.dev.yml): foreground build + up, so
#          the worker logs stream and Ctrl-C tears it down. (DESIGN.md §9)
#   prod → real GCP (docker-compose.yml): pull the pinned image, then recreate
#          detached so the old container drains gracefully. (DESIGN.md §10.2)
set -euo pipefail

STAGE="${1:-}"
if [ -z "$STAGE" ]; then
  echo "usage: $0 <dev|prod> [extra docker compose args]" >&2
  exit 2
fi
shift

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

# Sets COMPOSE_FILE + all worker env vars (and validates STAGE).
# shellcheck source=/dev/null
. "$HERE/env.sh"

COMPOSE_PATH="$REPO/$COMPOSE_FILE"
if [ ! -f "$COMPOSE_PATH" ]; then
  echo "[deploy] $COMPOSE_FILE not found at repo root." >&2
  echo "[deploy] Its spec lives in DESIGN.md §9 (dev) / §10 (prod); add it before deploying." >&2
  exit 1
fi

cd "$REPO"
echo "[deploy] stage=$STAGE project=$GCP_PROJECT_ID compose=$COMPOSE_FILE" >&2

case "$STAGE" in
  dev)
    exec docker compose -f "$COMPOSE_FILE" up --build "$@"
    ;;
  prod)
    docker compose -f "$COMPOSE_FILE" pull imagegen-worker
    exec docker compose -f "$COMPOSE_FILE" up -d "$@"
    ;;
esac
