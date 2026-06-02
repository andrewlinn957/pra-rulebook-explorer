#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python -m backend.rulebook_scraper.cli scrape \
  --all-parts \
  --include-glossary \
  --full-glossary \
  --include-crr-terms \
  --full-crr-terms \
  --all-guidance \
  --include-legal-instruments \
  --derive
