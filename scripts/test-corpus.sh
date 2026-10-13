#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# These integration tests require the local corpus database and source workbooks.
.venv/bin/python -m pytest -v \
  tests/test_xlsx_layout.py \
  tests/test_family_template_projection.py
