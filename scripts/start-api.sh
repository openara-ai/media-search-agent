#!/bin/bash
# Start API server only (command-line, not Docker)
# Usage: ./scripts/start-api.sh [--bind-host <addr>]
#   --bind-host <addr>  Host uvicorn binds to (default: 127.0.0.1)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BIND_HOST="127.0.0.1"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bind-host)   shift; BIND_HOST="$1" ;;
    --bind-host=*) BIND_HOST="${1#*=}" ;;
    -h|--help)
      echo "Usage: ./scripts/start-api.sh [--bind-host <addr>]"
      echo ""
      echo "Options:"
      echo "  --bind-host <addr>  Host uvicorn binds to (default: 127.0.0.1)"
      echo "                      Use 127.0.0.1 to restrict to localhost only."
      echo "                      Use 0.0.0.0 to accept connections from other machines."
      echo "  -h, --help          Show this help message and exit"
      exit 0
      ;;
  esac
  shift
done

cd "$PROJECT_ROOT"

# Check if virtual environment exists
if [ ! -d ".venv" ]; then
    echo "Error: Virtual environment not found. Please run: python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

# Activate virtual environment
source .venv/bin/activate

# Check if Qdrant is running
if ! curl -s http://localhost:6333/health > /dev/null 2>&1; then
    echo "⚠️  Warning: Qdrant doesn't appear to be running at http://localhost:6333"
    echo "   Start it with: docker run -d -p 6333:6333 -p 6334:6334 -v \$(pwd)/qdrant:/qdrant/storage qdrant/qdrant:latest"
    echo ""
fi

echo "🚀 Starting API server..."
echo "   API will be available at: http://localhost:8000"
echo "   Health check: http://localhost:8000/health"
echo ""

# Start uvicorn
exec uvicorn msa_apps.search_api.app:app --host "$BIND_HOST" --port 8000 --reload
