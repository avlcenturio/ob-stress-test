#!/usr/bin/env bash
# One-time local setup (Linux/macOS, Python 3.10+): creates .venv and installs everything.
set -e
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "Python 3 not found: install Python 3.10 or newer first."; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Python 3.10 or newer is needed (found $(python3 --version))."; exit 1; }
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
mkdir -p certs results
echo
locust -V
echo "Done. Start with:"
echo "  source .venv/bin/activate && locust -f locustfile.py"
echo "then open http://localhost:8089"
echo "Optional, for one-click PDF reports: pip install playwright && playwright install --with-deps chromium"
