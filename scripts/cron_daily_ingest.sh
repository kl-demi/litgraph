#!/bin/bash
# Cron wrapper for the full daily ingestion pipeline: fetch new arXiv + PubMed
# papers, enrich them with Semantic Scholar citation data, then extract
# PubTator3 entity mentions for any PubMed paper not yet processed. Supersedes
# running fetch-daily, fetch-daily-pubmed, enrich, and bio pubtator-mentions
# as separate manual steps -- one invocation covers the full pipeline.
#
# Does not include GO/Reactome/Disease Ontology ingestion (litgraph bio
# go-pathways/reactome-pathways/disease-ontology) -- those load static
# reference data and only need to be re-run occasionally (e.g. after an
# upstream release, see cron_pathway_release_check.sh), not daily.
#
# Cron runs with a bare environment and an unpredictable cwd, so this pins
# both explicitly -- litgraph's Settings loads `.env` relative to cwd, so
# without the `cd` it would silently fall back to defaults instead of the
# real ArcadeDB/API config.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p logs/cron
exec >> logs/cron/daily-ingest.log 2>&1

echo "=== $(date '+%Y-%m-%d %H:%M:%S') daily-ingest start ==="

echo "--- fetch-daily (arXiv) ---"
.venv/bin/litgraph fetch-daily

echo "--- fetch-daily-pubmed ---"
.venv/bin/litgraph fetch-daily-pubmed

echo "--- enrich (looping until caught up) ---"
while true; do
  out=$(.venv/bin/litgraph enrich --limit 500)
  echo "$out"
  echo "$out" | grep -q "Enriched 0 papers" && break
done

echo "--- PubTator3 mention extraction (looping until caught up) ---"
while true; do
  out=$(.venv/bin/litgraph bio pubtator-mentions)
  echo "$out"
  echo "$out" | grep -q "Processed 0 papers" && break
done

echo "=== $(date '+%Y-%m-%d %H:%M:%S') daily-ingest done ==="
