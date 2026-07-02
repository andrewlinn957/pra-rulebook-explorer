#!/usr/bin/env bash
set -euo pipefail
cd /root/.openclaw/workspace/projects/pra-rulebook-explorer
log="logs/llm-reference-full-pass-$(date +%Y%m%d-%H%M%S).log"
echo "$log" > logs/llm-reference-full-pass.latest
{
  echo "=== LLM reference full pass started $(date -Is) ==="
  echo "=== extraction ==="
  .venv/bin/python scripts/llm_reference_pass.py extract \
    --backend openclaw \
    --model openai-codex/gpt-5.5 \
    --workers 1 \
    --progress-every 25 \
    --max-chars 6000
  echo "=== resolution and edge insertion ==="
  .venv/bin/python scripts/llm_reference_pass.py resolve --add-edges
  echo "=== stats ==="
  .venv/bin/python scripts/llm_reference_pass.py stats
  echo "=== verify/export ==="
  PYTHONPATH=$PWD .venv/bin/python -m backend.rulebook_scraper.cli repair-internal-links --out backend/data/processed/graph.json
  ./scripts/verify_backend.py
  .venv/bin/python scripts/audit_graph_completeness.py
  echo "=== LLM reference full pass done $(date -Is) ==="
} 2>&1 | tee "$log"
