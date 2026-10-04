#!/bin/bash
# One command from a fresh Mac to a live voice agent on Fly.io.
#   bash scripts/go_live.sh            (first time; later: bash scripts/go_live.sh --deploy)
set -e
cd "$(dirname "$0")/.."

if grep -q '^LLM_API_KEY=$' .env 2>/dev/null; then
  echo ""
  echo "  OpenAI key is missing in .env."
  echo "  1) In the Browser pane, click Copy on your new OpenAI key."
  read -r -p "  2) Then press Enter here... " _
  pbpaste | python3 scripts/env_set.py LLM_API_KEY
fi

if ! command -v fly >/dev/null 2>&1 && ! command -v flyctl >/dev/null 2>&1 && [ ! -x "$HOME/.fly/bin/flyctl" ]; then
  echo "  Installing flyctl..."
  if command -v brew >/dev/null 2>&1; then brew install flyctl; else curl -fsSL https://fly.io/install.sh | sh; fi
fi

python3 scripts/deploy_fly.py "$@"
