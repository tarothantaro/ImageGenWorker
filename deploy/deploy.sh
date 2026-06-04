#!/usr/bin/env bash
# Single entry point that brings up the image-gen worker for a given stage.
# Dispatches into the per-stage layout under deploy/stages/<stage>/ (mirrors
# ../Application's "everything per-stage" convention).
#
# Usage:
#   ./deploy.sh <stage> [extra args...]
#     stage : dev | prod
#
#   dev  → emulator stack wired to the Application local stack's Pub/Sub
#          (deploy/stages/dev/up.sh): detached + --wait, then tail/smoke.
#          (DESIGN.md §9)
#   prod → real GCP (deploy/stages/prod/deploy.sh): pull the pinned image, then
#          recreate detached so the old container drains. (DESIGN.md §10.2)
#
# Per-stage config + compose live in deploy/stages/<stage>/. Run those scripts
# directly for finer control (dev also has down.sh + smoke.sh).
set -euo pipefail

STAGE="${1:-}"
if [ -z "$STAGE" ]; then
  echo "usage: $0 <dev|prod> [extra args]" >&2
  exit 2
fi
shift

HERE="$(cd "$(dirname "$0")" && pwd)"

case "$STAGE" in
  dev)
    exec "$HERE/stages/dev/up.sh" "$@"
    ;;
  prod)
    exec "$HERE/stages/prod/deploy.sh" "$@"
    ;;
  *)
    echo "[deploy] unknown STAGE=$STAGE (expected dev|prod)" >&2
    exit 2
    ;;
esac
