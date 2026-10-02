#!/usr/bin/env bash
set -euo pipefail

# ── Ace Stream player launcher ─────────────────────────────────────────────────
# Usage:
#   ./ace_player.sh <acestream-hash>
#   ./ace_player.sh acestream://<hash>
#   ./ace_player.sh                         ← prompts for hash interactively
#
# Requires: Docker running, VLC installed

ENGINE_PORT=6878
CONTAINER_NAME=acestream-engine
IMAGE=vstavrinov/acestream-engine:latest

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}▶${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC}  $*"; }
err()  { echo -e "${RED}✖${NC}  $*" >&2; }

# ── Parse input ───────────────────────────────────────────────────────────────
RAW="${1:-}"
if [ -z "$RAW" ]; then
  read -rp "Enter Ace Stream hash or acestream:// URL: " RAW
fi

# Strip acestream:// prefix if present
HASH="${RAW#acestream://}"
HASH="${HASH#//}"  # handle acestream:////hash edge case
HASH="$(echo "$HASH" | tr -d '[:space:]')"

if [ -z "$HASH" ]; then
  err "No hash provided. Exiting."
  exit 1
fi

if ! [[ "$HASH" =~ ^[0-9a-fA-F]{40}$ ]]; then
  err "Not a valid Ace Stream content ID (expected 40 hex characters)."
  exit 1
fi

# ── Verify Docker is running ──────────────────────────────────────────────────
if ! docker info &>/dev/null; then
  err "Docker is not running. Open Docker Desktop and try again."
  exit 1
fi

# ── Start or reuse engine container ──────────────────────────────────────────
if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  info "Ace Stream engine already running"
else
  if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    info "Restarting existing engine container..."
    docker start "$CONTAINER_NAME" >/dev/null
  else
    info "Starting Ace Stream engine..."
    docker run -d \
      --name "$CONTAINER_NAME" \
      --restart unless-stopped \
      --platform linux/amd64 \
      -p "127.0.0.1:${ENGINE_PORT}:${ENGINE_PORT}" \
      "$IMAGE" >/dev/null
  fi
fi

# ── Wait for engine to be ready ────────────────────────────────────────────────
info "Waiting for engine on port ${ENGINE_PORT}..."
MAX=30; COUNT=0
until curl -sf "http://127.0.0.1:${ENGINE_PORT}/webui/api/service?method=get_version" >/dev/null 2>&1; do
  sleep 1
  COUNT=$((COUNT+1))
  if [ "$COUNT" -ge "$MAX" ]; then
    err "Engine did not respond after ${MAX}s. Check: docker logs ${CONTAINER_NAME}"
    exit 1
  fi
done
info "Engine ready"

# ── Build stream URL and open VLC ─────────────────────────────────────────────
STREAM_URL="http://127.0.0.1:${ENGINE_PORT}/ace/getstream?id=${HASH}"
info "Opening stream in VLC: $STREAM_URL"

if [ -d "/Applications/VLC.app" ] || [ -d "$HOME/Applications/VLC.app" ] || open -Ra VLC 2>/dev/null; then
  open -a VLC "$STREAM_URL"
else
  err "VLC not found. Install it with: brew install --cask vlc"
  echo "Stream URL (open manually in any player): $STREAM_URL"
  exit 1
fi

echo ""
echo "──────────────────────────────────────────────────────────────"
echo " Stream started. VLC may buffer for 10–30s on first load."
echo " Engine container '${CONTAINER_NAME}' keeps running in the background."
echo " Stop it with: docker stop ${CONTAINER_NAME}"
echo "──────────────────────────────────────────────────────────────"
