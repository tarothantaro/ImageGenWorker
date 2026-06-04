#!/usr/bin/env bash
# End-to-end dev smoke check: seed GCS → publish a job → read the worker's
# completion off job-completed. Runs smoke.py inside a throwaway worker
# container so it shares the worker's env + network (reaches the emulators by
# service name). The stack must be up first (./up.sh).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/env.sh"

echo "[dev/smoke] running smoke.py inside the worker image..." >&2
# --no-deps: don't spin up mock-comfyui/fake-gcs again (they're already up).
# -e GCS_BUCKET: smoke.py reads it; the rest of the env comes from the service.
exec docker compose -f "$HERE/docker-compose.yml" run --rm --no-deps \
  -e GCS_BUCKET="$GCS_BUCKET" \
  -v "$HERE/smoke.py:/app/smoke.py:ro" \
  imagegen-worker python /app/smoke.py
