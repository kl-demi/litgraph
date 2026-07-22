#!/bin/bash
# Cron wrapper for `litgraph fetch-daily` (arXiv). Cron runs with a bare
# environment and an unpredictable cwd, so this pins both explicitly --
# litgraph's Settings loads `.env` relative to cwd, so without the `cd` it
# would silently fall back to defaults instead of the real ArcadeDB/API config.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p logs/cron
exec >> logs/cron/fetch-daily.log 2>&1

echo "=== $(date '+%Y-%m-%d %H:%M:%S') fetch-daily start ==="
.venv/bin/litgraph fetch-daily
echo "=== $(date '+%Y-%m-%d %H:%M:%S') fetch-daily done ==="
