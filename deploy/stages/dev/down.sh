#!/usr/bin/env bash
# Tear down the dev worker stack. Pass -v to also drop volumes (none defined
# today, but kept for parity with the Application local stack's down.sh).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/env.sh"

echo "[dev/down] stopping dev worker stack" >&2
docker compose -f "$HERE/docker-compose.yml" down "$@"
