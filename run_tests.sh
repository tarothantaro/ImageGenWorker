#!/usr/bin/env bash
set -euo pipefail
PYTHON=~/python_env/torch-env/bin/python
cd "$(dirname "$0")"

# Keep image-gen-contract install in sync with the sibling repo's source.
"$PYTHON" -m pip install -e "$(pwd)/../ImageGenContract" --quiet

exec "$PYTHON" -m pytest tests/unit/ --cov=imagegen --cov-report=term-missing "$@"
