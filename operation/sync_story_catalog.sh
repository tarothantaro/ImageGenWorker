#!/usr/bin/env bash
# Single entry point for the bound-story catalog sync (mirrors deploy/deploy.sh).
# Dispatches into the per-stage wrappers under operation/stages/<stage>/.
#
# Usage:
#   ./sync_story_catalog.sh <stage> [extra args...]
#     stage : dev | preprod | prod
#
#   dev     → the Application local stack's Firestore emulator (must be up).
#   preprod → real Firestore on tarostory-preprod (worker SA / ADC).
#   prod    → real Firestore on tarostory-prod (worker SA / ADC).
#
# Extra args pass through (e.g. --template 4, --dry-run). Run the per-stage
# wrappers directly for finer control.
set -euo pipefail

STAGE="${1:-}"
if [ -z "$STAGE" ]; then
  echo "usage: $0 <dev|preprod|prod> [--template ID] [--dry-run]" >&2
  exit 2
fi
shift

HERE="$(cd "$(dirname "$0")" && pwd)"
WRAPPER="$HERE/stages/$STAGE/sync_story_catalog.sh"
if [ ! -x "$WRAPPER" ]; then
  echo "[sync] unknown STAGE=$STAGE (expected dev|preprod|prod)" >&2
  exit 2
fi
exec "$WRAPPER" "$@"
