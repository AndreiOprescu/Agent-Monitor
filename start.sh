#!/usr/bin/env bash
set -euo pipefail

# Preflight: make sure the tools we need are present and authenticated.
command -v claude >/dev/null || { echo "ERROR: 'claude' CLI not found on PATH."; exit 1; }
command -v gh     >/dev/null || { echo "ERROR: 'gh' CLI not found on PATH."; exit 1; }
command -v git    >/dev/null || { echo "ERROR: 'git' not found on PATH."; exit 1; }

gh auth status >/dev/null 2>&1 || { echo "ERROR: run 'gh auth login' first."; exit 1; }
git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || { echo "ERROR: run this from inside your git repository."; exit 1; }

# Recommended for a 2-agent fleet: bill against the API, not a subscription seat.
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "WARNING: ANTHROPIC_API_KEY is not set — a 2-agent loop on a subscription"
  echo "         seat will exhaust its limits fast. See README (Cost)."
fi

python3 -m pip install -q -r requirements.txt
exec python3 orchestrator.py
