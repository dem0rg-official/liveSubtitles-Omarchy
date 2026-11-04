#!/usr/bin/env bash
# Live Subtitles uninstaller.
set -euo pipefail

PLUGIN_ID="dem0rg.livesubtitles"
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${LS_VENV:-$HOME/.local/share/livesubtitles/venv}"

systemctl --user disable --now livesubtitles.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/livesubtitles.service"
systemctl --user daemon-reload 2>/dev/null || true

# Take the widget out of the bar layout.
omarchy bar move "$PLUGIN_ID" --section none >/dev/null 2>&1 || true
rm -f "$HOME/.config/omarchy/plugins/$PLUGIN_ID"

rm -f "$HOME/.local/bin/livesubtitles-cli" "$HOME/.local/bin/livesubtitles-service"

echo "Keep the venv and models? [y/N]"
read -r answer
if [[ ! "$answer" =~ ^[Yy]$ ]]; then
  rm -rf "$VENV" "$HOME/.local/share/livesubtitles"
  echo "Removed venv, models and state."
fi

echo "Config kept at ~/.config/livesubtitles/ (delete manually if unwanted)."
echo "Live Subtitles uninstalled."
