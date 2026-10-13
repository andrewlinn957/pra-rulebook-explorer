#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

.venv/bin/python -m pytest tests -v \
  --ignore=tests/test_xlsx_layout.py \
  --ignore=tests/test_family_template_projection.py
npm --prefix frontend test
npm --prefix frontend run build
