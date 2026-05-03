#!/usr/bin/env bash
# Media Search Agent — Development CLI
#
# Repeatable developer entrypoint for this repo. It activates the project venv,
# exports MSA_DEV=1, and then either opens an interactive shell or runs the
# command you pass in that environment.
#
# Usage:
#   bash scripts/dev-cli.sh
#   bash scripts/dev-cli.sh ./scripts/start.sh
#   bash scripts/dev-cli.sh pytest

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MSA_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ACTIVATE="$MSA_ROOT/.venv/bin/activate"

if [[ ! -f "$ACTIVATE" ]]; then
  echo "ERROR: Python virtual environment not found at $ACTIVATE" >&2
  echo "Create it first, then run the bootstrap steps from README:" >&2
  echo "  python -m venv .venv" >&2
  echo "  source .venv/bin/activate" >&2
  echo "  pip install -r requirements.txt" >&2
  echo "  ./scripts/dev-setup.sh" >&2
  exit 1
fi

# Clear any MSA_ vars that may have been exported by an end-user install session
# in the parent shell (e.g. from running `msa api start` from the system install).
# Without this, MSA_CONFIG_PATH (pointing to ~/Library/Application Support/...)
# overrides the repo's config.yaml and causes start.sh to bind to port 8000.
unset MSA_CONFIG_PATH MSA_DATA_DIR MSA_LOG_DIR MSA_CACHE_DIR MSA_VENV_DIR

if [[ $# -eq 0 ]]; then
  DEV_RCFILE="$(mktemp "${TMPDIR:-/tmp}/msa-dev-shell.XXXXXX")"
  cat > "$DEV_RCFILE" <<EOF
# Ensure a clean MSA environment — clear any vars from end-user install
unset MSA_CONFIG_PATH MSA_DATA_DIR MSA_LOG_DIR MSA_CACHE_DIR MSA_VENV_DIR
export MSA_DEV=1
export VIRTUAL_ENV_DISABLE_PROMPT=1
if [[ -f "\$HOME/.bashrc" ]]; then
  source "\$HOME/.bashrc"
fi
source "$ACTIVATE"
PS1='(msa-dev) '"\${PS1:-}"
export PS1
export VIRTUAL_ENV_PROMPT='(msa-dev) '
echo "Media Search Agent dev shell"
echo "  root: $MSA_ROOT"
echo "  venv: \${VIRTUAL_ENV:-$MSA_ROOT/.venv}"
echo "  MSA_DEV=\$MSA_DEV"
rm -f "$DEV_RCFILE"
EOF

  exec bash --rcfile "$DEV_RCFILE" -i
fi

export VIRTUAL_ENV_DISABLE_PROMPT=1
source "$ACTIVATE"
export MSA_DEV=1

exec "$@"
