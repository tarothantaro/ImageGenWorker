#!/usr/bin/env bash
# Sync bound-story metadata into preprod's REAL Firestore (tarostory-preprod).
# An operation script (operation/stages/<stage>/), per the "everything
# per-stage" convention: sources the canonical deploy/stages/preprod/env.sh for
# GCP_PROJECT_ID + SA_KEY_FILE, then runs the shared
# operation/sync_story_catalog.py over the worker service account.
#
# The API server then serves the synced title/lesson plus the per-panel
# storybook story_text (the `story-text` skill's `texts`) via
# GET /api/v1/templates/{id}. Idempotent; merges onto the seed's catalog docs.
#   ./sync_story_catalog.sh
#   ./sync_story_catalog.sh --template 4
#   ./sync_story_catalog.sh --dry-run
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
STAGE_NAME="$(basename "$HERE")"
. "$HERE/../../../deploy/stages/$STAGE_NAME/env.sh"   # canonical stage env: GCP_PROJECT_ID

if [ -n "${FIRESTORE_EMULATOR_HOST:-}" ]; then
  echo "[$STAGE_NAME/sync] FIRESTORE_EMULATOR_HOST is set — this targets REAL "\
"Firestore; unset it (or use the dev wrapper)." >&2
  exit 2
fi

export GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT:-$GCP_PROJECT_ID}"
# Auth runs as the OPERATOR, not the worker. The worker SA deliberately has no
# Firestore access (DESIGN.md §4.3), so writing the catalog uses operator ADC —
# the same identity + permission the Application's seed_catalog.sh needs
# (`gcloud auth application-default login`, Firestore admin). Set
# GOOGLE_APPLICATION_CREDENTIALS to override with a key.
echo "[$STAGE_NAME/sync] project=$GOOGLE_CLOUD_PROJECT creds=${GOOGLE_APPLICATION_CREDENTIALS:-operator ADC}" >&2
# `python` = the interpreter with the catalog extra (google-cloud-firestore);
# override with PYTHON=... (install: pip install -e .[catalog]).
exec "${PYTHON:-python}" "$HERE/../../sync_story_catalog.py" "$@"
