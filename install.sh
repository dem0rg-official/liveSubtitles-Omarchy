#!/usr/bin/env bash
# Live Subtitles for Omarchy — installer.
# Checks dependencies, creates a Python venv with faster-whisper + Argos
# Translate, installs CLI wrappers and the systemd user unit, registers the
# Omarchy shell plugin, and pre-downloads offline models.
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${LS_VENV:-$HOME/.local/share/livesubtitles/venv}"
DATA="$HOME/.local/share/livesubtitles"
CONFIG="$HOME/.config/livesubtitles"
BIN="$HOME/.local/bin"
PLUGIN_ID="dem0rg.livesubtitles"
PLUGIN_DIR="$HOME/.config/omarchy/plugins/$PLUGIN_ID"

err() { echo -e "\033[31m✗\033[0m $*" >&2; }
ok()  { echo -e "\033[32m✓\033[0m $*"; }
info(){ echo -e "\033[36m·\033[0m $*"; }

echo "── Live Subtitles installer ──────────────────────────────"

# ------------------------------------------------------------- dependencies
missing=()
for cmd in python3 pactl systemctl quickshell; do
  command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
done
if ((${#missing[@]})); then
  err "Missing commands: ${missing[*]}"
  err "Install them first, e.g.: omarchy pkg add pipewire-pulse (quickshell ships with Omarchy)"
  exit 1
fi
ok "python3, pactl, systemctl, quickshell found"

PYTHON=python3
PYVER="$($PYTHON -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
info "Python $PYVER"

# ------------------------------------------------------------------- venv
if [[ ! -x "$VENV/bin/python" ]]; then
  info "Creating venv at $VENV …"
  mkdir -p "$(dirname "$VENV")"
  $PYTHON -m venv "$VENV"
fi
ok "venv ready"

info "Installing Python packages (faster-whisper, argostranslate, numpy) …"
"$VENV/bin/pip" install --upgrade pip >/dev/null
# CPU-only PyTorch first: argostranslate pulls in torch, and the default wheel
# would drag in ~2 GB of NVIDIA CUDA libraries. CPU torch is ~200 MB and all
# we need — translation runs on CPU either way.
"$VENV/bin/pip" install torch --index-url https://download.pytorch.org/whl/cpu
"$VENV/bin/pip" install faster-whisper argostranslate sherpa-onnx numpy
ok "Python dependencies installed (CPU-only torch)"

# -------------------------------------------------------- CLI wrappers
mkdir -p "$BIN" "$DATA" "$CONFIG"
install -m 755 "$BASE/bin/livesubtitles-cli" "$BIN/livesubtitles-cli"
install -m 755 "$BASE/bin/livesubtitles-service" "$BIN/livesubtitles-service"
install -m 755 "$BASE/bin/livesubtitles-waybar-status" "$BIN/livesubtitles-waybar-status"
# Point installed wrappers back at this checkout even if it moves.
sed -i "s|^BASE=.*|BASE=\"$BASE\"|" "$BIN/livesubtitles-cli" "$BIN/livesubtitles-service"
ok "CLI installed to ~/.local/bin"

# -------------------------------------------------------- systemd user unit
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
sed "s|%h|$HOME|; s|Projects/liveSubtitles-Omarchy|${BASE#$HOME/}|" \
  "$BASE/packaging/livesubtitles.service" > "$UNIT_DIR/livesubtitles.service"
systemctl --user daemon-reload
systemctl --user enable --now livesubtitles.service
ok "systemd user service enabled (livesubtitles.service)"

# -------------------------------------------------------- Omarchy plugin
mkdir -p "$HOME/.config/omarchy/plugins"
if [[ -L "$PLUGIN_DIR" ]]; then
  ln -sfn "$BASE/omarchy-plugin" "$PLUGIN_DIR"
else
  # Existing real copy (e.g. from a previous manual install): sync contents
  # over it so the shell keeps its scanned path.
  info "Syncing existing plugin copy at $PLUGIN_DIR"
  cp -f "$BASE/omarchy-plugin/"*.qml "$BASE/omarchy-plugin/manifest.json" "$PLUGIN_DIR/"
fi
omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
# Third-party plugins install disabled by design (review-before-enable);
# enable and place the widget in the bar explicitly.
omarchy plugin enable "$PLUGIN_ID" >/dev/null 2>&1 || true
ok "Omarchy plugin registered at $PLUGIN_DIR"

# -------------------------------------------------------- offline models
info "Downloading offline models (Whisper small ~460 MB, may take a while) …"
if "$VENV/bin/python" - <<'PY'
from faster_whisper import WhisperModel
WhisperModel("small", device="cpu", compute_type="int8")
print("whisper-small cached")
PY
then
  ok "Whisper small cached (offline from now on)"
else
  err "Whisper download failed — the service will retry at first run"
fi

# Baseline pairs so the very first run works; add more with `argospm install
# translate-<src>_<dst>` (~50–100 MB per pair, offline once cached).
if "$VENV/bin/argospm" update >/dev/null 2>&1 \
   && "$VENV/bin/argospm" install translate-en_ru >/dev/null 2>&1 \
   && "$VENV/bin/argospm" install translate-ru_en >/dev/null 2>&1; then
  ok "Argos packs installed: en↔ru"
else
  info "Argos pack install failed or was skipped — add pairs manually:"
  info "  $VENV/bin/argospm update"
  info "  $VENV/bin/argospm install translate-en_ru translate-ru_en"
fi

# ------------------------------------------------- streaming ASR model (en)
# sherpa-onnx streaming zipformer: live hypothesis updated every 100 ms of
# speech — near-zero subtitle latency for English. Whisper still handles
# other languages and the accurate final pass.
STREAM_DIR="$DATA/models/sherpa-streaming-en"
STREAM_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-en-2023-06-26.tar.bz2"
if [[ -f "$STREAM_DIR/tokens.txt" ]]; then
  ok "Streaming zipformer already cached"
else
  info "Downloading streaming zipformer (en, ~80 MB) …"
  TMP=$(mktemp -d)
  if curl -fsSL "$STREAM_URL" -o "$TMP/m.tar.bz2" \
     && tar xjf "$TMP/m.tar.bz2" -C "$TMP" \
     && mkdir -p "$STREAM_DIR"; then
    EXTRACTED=$(find "$TMP" -maxdepth 1 -type d -name "sherpa-onnx-streaming-*" | head -1)
    cp "$EXTRACTED/tokens.txt" "$STREAM_DIR/"
    cp "$EXTRACTED"/*encoder*.int8.onnx "$STREAM_DIR/encoder.onnx"
    cp "$EXTRACTED"/*decoder*.int8.onnx "$STREAM_DIR/decoder.onnx"
    cp "$EXTRACTED"/*joiner*.int8.onnx "$STREAM_DIR/joiner.onnx"
    ok "Streaming zipformer cached (English live captions enabled)"
  else
    info "Streaming model download failed — service falls back to whisper-only"
  fi
  rm -rf "$TMP"
fi

# -------------------------------------------------------- bar layout
info "Adding the LS widget to the bar (right section) …"
omarchy bar move dem0rg.livesubtitles --section right >/dev/null 2>&1 \
  && ok "Widget placed (omarchy bar move dem0rg.livesubtitles --section right)" \
  || info "Place it manually: omarchy bar move dem0rg.livesubtitles --section right"

echo
ok "Done. The LS button appears in the bar; click it to start subtitles."
info "CLI: livesubtitles-cli toggle | lang ru | source auto | status"
info "Language packs: $VENV/bin/argospm install translate-en_de (etc.)"
info "Logs: journalctl --user -u livesubtitles -f"
