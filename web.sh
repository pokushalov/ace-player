#!/usr/bin/env bash
# Start the Ace Player web UI and open it in the browser.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${ACE_PORT:-8888}"

if ! python3 --version >/dev/null 2>&1; then
  echo "python3 not found. Install the Xcode Command Line Tools: xcode-select --install" >&2
  exit 1
fi

# Open the browser as soon as the server answers (up to ~10 s).
(
  for _ in $(seq 1 50); do
    if curl -fs "http://localhost:${PORT}/" >/dev/null 2>&1; then
      open "http://localhost:${PORT}"
      exit 0
    fi
    sleep 0.2
  done
) &

exec python3 "$DIR/app.py"
