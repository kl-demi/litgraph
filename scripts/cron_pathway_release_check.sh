#!/bin/bash
# Cron wrapper for scripts/check_pathway_releases.py: checks GO's and Reactome's
# current release against the last-seen one (data/pathway_release_state.json) and
# only re-ingests a source if its release changed. A no-op most days.
#
# See cron_fetch_daily.sh for why cwd is pinned before running anything.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p logs/cron
exec >> logs/cron/pathway-release-check.log 2>&1

echo "=== $(date '+%Y-%m-%d %H:%M:%S') pathway-release-check start ==="
.venv/bin/python scripts/check_pathway_releases.py
echo "=== $(date '+%Y-%m-%d %H:%M:%S') pathway-release-check done ==="
