#!/bin/sh
# One-shot setup on the cron host: venv, dependencies, database, then the crontab to install.
# Idempotent. Does NOT touch your crontab -- you install it yourself after `fares alert-test`.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e ".[dashboard]"
mkdir -p data ops
.venv/bin/fares migrate

if [ ! -f .env ]; then
    echo
    echo "No .env yet: cp .env.example .env and fill in SERPAPI_KEY, NTFY_TOPIC, ..."
fi
echo
echo "Next: .venv/bin/fares alert-test   (must reach your phone before you install cron)"
echo "Then add these lines with \`crontab -e\`:"
echo
sed "s|/opt/flight-arbitrage-agent|$ROOT|g" scripts/crontab.example
