#!/bin/bash
# Cron wrapper for `litgraph fetch-daily-pubmed`. See cron_fetch_daily.sh for
# why cwd is pinned before invoking litgraph.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p logs/cron
exec >> logs/cron/fetch-daily-pubmed.log 2>&1

echo "=== $(date '+%Y-%m-%d %H:%M:%S') fetch-daily-pubmed start ==="
.venv/bin/litgraph fetch-daily-pubmed
echo "=== $(date '+%Y-%m-%d %H:%M:%S') fetch-daily-pubmed done ==="
