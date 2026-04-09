#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  "$ROOT_DIR/.venv/bin/python" build_executable.py
elif command -v python3 >/dev/null 2>&1; then
  python3 build_executable.py
elif command -v python >/dev/null 2>&1; then
  python build_executable.py
else
  echo "Python not found. Install Python 3.11+." >&2
  exit 1
fi
