#!/usr/bin/env bash
set -euo pipefail
PYTHON=~/python_env/torch-env/bin/python
cd "$(dirname "$0")"
exec "$PYTHON" -m pytest tests/unit/ --cov=imagegen --cov-report=term-missing "$@"
