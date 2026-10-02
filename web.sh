#!/usr/bin/env bash
# Start the Ace Player web UI and open it in the browser.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

if ! python3 --version >/dev/null 2>&1; then
  echo "python3 not found. Install the Xcode Command Line Tools: xcode-select --install" >&2
  exit 1
fi

# The app picks another free port when ${PORT} is taken, so it opens the browser itself (--open)
# on whatever port it actually got, instead of this script guessing.
exec python3 "$DIR/app.py" --open
