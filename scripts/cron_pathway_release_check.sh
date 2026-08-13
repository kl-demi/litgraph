#!/bin/bash
# Cron wrapper for `litgraph bio check-releases`: checks GO's, Reactome's, and
# Disease Ontology's current release against the last-seen one (one state file
# per ARCADEDB_DATABASE, see spokebio/release_check.py) and only re-ingests a
# source if its release changed. A no-op most days.
#
# See cron_fetch_daily.sh for why cwd is pinned before running anything.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p logs/cron
exec >> logs/cron/pathway-release-check.log 2>&1

echo "=== $(date '+%Y-%m-%d %H:%M:%S') pathway-release-check start ==="
.venv/bin/litgraph bio check-releases
echo "=== $(date '+%Y-%m-%d %H:%M:%S') pathway-release-check done ==="
