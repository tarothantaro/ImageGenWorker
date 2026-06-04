#!/usr/bin/env bash
# Deploy / upgrade the prod worker (DESIGN.md §10.2): pull the pinned image,
# then recreate detached so the old container drains gracefully (SIGTERM →
# stop_grace_period). Extra args pass through to `docker compose up`.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/env.sh"

if [ ! -f "$SA_KEY_FILE" ]; then
  echo "[prod/deploy] SA key not found at $SA_KEY_FILE (DESIGN.md §10.3)." >&2
  echo "[prod/deploy] Provision it, or set SA_KEY_FILE=/path/to/sa.json." >&2
  exit 1
fi

echo "[prod/deploy] project=$GCP_PROJECT_ID image=$IMAGE comfyui=$COMFYUI_URL" >&2

docker compose -f "$HERE/docker-compose.yml" pull imagegen-worker
exec docker compose -f "$HERE/docker-compose.yml" up -d "$@"
