#!/usr/bin/env bash
set -euo pipefail

# Run the project's test suite in the current repository.
# Usage:
#   ./scripts/run-tests.sh            # run tests with repo venv python if present
#   ./scripts/run-tests.sh --install # install requirements before running

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$REPO_ROOT/.venv/bin/python"

# Prefer repo virtualenv python if it exists, otherwise fall back to system python
if [ -x "$VENV_PY" ]; then
  PYTHON="$VENV_PY"
else
  PYTHON="$(command -v python3 || command -v python)"
fi

if [ "$#" -ge 1 ] && [ "$1" = "--install" ]; then
  echo "Installing requirements into ${PYTHON}..."
  "$PYTHON" -m pip install -U pip
  "$PYTHON" -m pip install -r "$REPO_ROOT/requirements.txt"
  shift
fi

echo "Running pytest with ${PYTHON}..."
exec "$PYTHON" -m pytest -q "$@"
