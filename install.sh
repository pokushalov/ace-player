#!/usr/bin/env bash
set -euo pipefail

# ── Ace Stream on macOS — one-time setup ──────────────────────────────────────
# Installs: Homebrew (if missing), Docker Desktop, VLC. Optional: Ace Link (--with-ace-link)

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}▶${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC}  $*"; }

# 1. Homebrew ──────────────────────────────────────────────────────────────────
if ! command -v brew &>/dev/null; then
  info "Installing Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
else
  info "Homebrew already installed — skipping"
fi

# 2. Docker Desktop ────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
  info "Installing Docker Desktop..."
  brew install --cask docker
  warn "Docker Desktop installed. Open it from /Applications and let it finish starting before running ace_player.sh"
else
  info "Docker already installed — skipping"
fi

# 3. VLC ───────────────────────────────────────────────────────────────────────
if [ ! -d "/Applications/VLC.app" ]; then
  info "Installing VLC..."
  brew install --cask vlc
else
  info "VLC already installed — skipping"
fi

# 4. Ace Link (optional) ───────────────────────────────────────────────────────
# Only needed to open acestream:// links from other apps. The web UI (./web.sh) does not need it.
# It is unsigned, so macOS blocks it; installing it also removes the quarantine flag, hence opt-in:
#   ./install.sh --with-ace-link
if [ "${1:-}" = "--with-ace-link" ]; then
  if ! brew list --cask ace-link &>/dev/null 2>&1; then
    info "Installing Ace Link..."
    brew install --cask ace-link
  else
    info "Ace Link already installed — skipping"
  fi
  ACE_LINK_APP="/Applications/Ace Link.app"
  if [ -d "$ACE_LINK_APP" ]; then
    info "Removing Gatekeeper quarantine from Ace Link (it is unsigned)..."
    xattr -rd com.apple.quarantine "$ACE_LINK_APP" 2>/dev/null || true
  fi
else
  info "Skipping Ace Link (optional; pass --with-ace-link to install it)"
fi

# 5. Python 3 ──────────────────────────────────────────────────────────────────
if ! xcode-select -p &>/dev/null; then
  warn "Xcode Command Line Tools are missing (they provide python3). Run: xcode-select --install"
fi

echo ""
echo "──────────────────────────────────────────────────────────────"
echo " Setup complete."
echo " • Make sure Docker Desktop is running before launching."
echo " • Run ./ace_player.sh <acestream-id-or-hash> to start a stream."
echo "──────────────────────────────────────────────────────────────"
