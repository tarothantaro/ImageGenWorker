#!/usr/bin/env bash
# Build + (re)start the preprod worker against tarostory-preprod.
# Reuses the prod compose file (the runtime shape is identical — see
# env.sh for the three preprod differences) under its own compose
# project name, so prod and preprod workers can coexist on one host.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
. "$HERE/env.sh"

if [ ! -f "$SA_KEY_FILE" ]; then
  echo "[preprod/deploy] SA key not found at $SA_KEY_FILE" >&2
  echo "[preprod/deploy] gcloud iam service-accounts keys create ... --iam-account=imagegen-worker@${GCP_PROJECT_ID}.iam.gserviceaccount.com" >&2
  exit 1
fi

echo "[preprod/deploy] project=$GCP_PROJECT_ID image=$IMAGE comfyui=$COMFYUI_URL" >&2

docker build -t "$IMAGE" "$REPO"
exec docker compose -p imagegen-worker-preprod \
  -f "$HERE/../prod/docker-compose.yml" up -d "$@"
