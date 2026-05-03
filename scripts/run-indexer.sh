#!/usr/bin/env bash
set -euo pipefail
source .venv/bin/activate
msa index run --media-source-override "${1:-/home/kumar/projects/media-search-agent/data/sample_photos}"
