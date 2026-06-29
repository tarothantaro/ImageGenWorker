#!/usr/bin/env bash
# Sync bound-story metadata into the dev stack's Firestore (the Application
# local emulator). An operation script (operation/stages/<stage>/), per the
# project's "everything per-stage" convention: sources the canonical
# deploy/stages/dev/env.sh for GCP_PROJECT_ID, then runs the shared
# operation/sync_story_catalog.py against the emulator.
#
# The Application local stack must be up (it owns the Firestore emulator on
# host port 8200 — see Application/server/deploy/stages/local/env.sh). The API
# server then serves the synced title/lesson plus the per-panel storybook
# story_text (the `story-text` skill's `texts`) via GET /api/v1/templates/{id}.
#   ./sync_story_catalog.sh                 # every bound template
#   ./sync_story_catalog.sh --template 4
#   ./sync_story_catalog.sh --dry-run
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
STAGE_NAME="$(basename "$HERE")"
. "$HERE/../../../deploy/stages/$STAGE_NAME/env.sh"   # canonical stage env: GCP_PROJECT_ID

# The Firestore client keys off GOOGLE_CLOUD_PROJECT; the emulator off
# FIRESTORE_EMULATOR_HOST. dev points at the Application local stack's emulator
# on the host (its FIRESTORE_EMULATOR_PORT defaults to 8200). Override either if
# your local stack differs.
export GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT:-$GCP_PROJECT_ID}"
export FIRESTORE_EMULATOR_HOST="${FIRESTORE_EMULATOR_HOST:-localhost:8200}"

# This operation script runs on the host, while deploy/stages/dev/env.sh also
# configures container runtime values. The container-only fake-gcs-server DNS
# name is not resolvable from the host; use the Application stack's published
# localhost port for catalog example uploads.
if [ "${STORAGE_EMULATOR_HOST:-}" = "http://fake-gcs-server:4443" ]; then
  export STORAGE_EMULATOR_HOST="http://localhost:4443"
fi

# Catalog example panels upload to the LOCAL stack's fake-gcs bucket (the only
# bucket it creates is "$GOOGLE_CLOUD_PROJECT-images"). Pass it as an explicit
# --examples-bucket flag — which wins in sync_story_catalog.py over the
# GCS_BUCKET/GCS_INPUT_BUCKET fallback — so a GCS_BUCKET left over in the shell
# from a sourced preprod/prod env.sh can't redirect uploads at a cloud bucket
# that doesn't exist in fake-gcs (a 404 that aborts the whole sync). Mirrors the
# preprod/prod wrappers' --examples-bucket pin.
export EXAMPLES_BUCKET="${EXAMPLES_BUCKET:-${GOOGLE_CLOUD_PROJECT}-images}"

echo "[dev/sync] project=$GOOGLE_CLOUD_PROJECT firestore=$FIRESTORE_EMULATOR_HOST examples_bucket=$EXAMPLES_BUCKET" >&2
# `python` = the interpreter with the catalog extra (google-cloud-firestore);
# override with PYTHON=... (install: pip install -e .[catalog]). The wrapper's
# --examples-bucket comes first so an explicit one in "$@" still wins.
exec "${PYTHON:-python}" "$HERE/../../sync_story_catalog.py" \
  --examples-bucket "$EXAMPLES_BUCKET" "$@"
