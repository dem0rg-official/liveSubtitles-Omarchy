#!/usr/bin/env python3
"""CLI for the Live Subtitles service (control.fifo + state.json)."""

import json
import os
import sys

STATE_DIR = os.path.expanduser("~/.local/share/livesubtitles")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
FIFO_FILE = os.path.join(STATE_DIR, "control.fifo")

USAGE = """usage: livesubtitles-cli <command>

commands:
  toggle            flip subtitles on/off (persists)
  enable | disable  set state explicitly
  lang <code>       target language: en ru uk es fr de it pt pl tr ja zh ko
  source <code>     spoken language: auto (detect) or a fixed code
  original [on|off] show the untranslated line above the translation
  speed <profile>   battery | balanced | turbo (CPU spent for lower latency)
  opacity <0.2-1.0> subtitle overlay opacity
  capture <target>  system | si<N> (app id from the apps command)
  apps              list apps currently playing sound (capture targets)
  status            print state as JSON
"""

LANGUAGES = ["en", "ru", "uk", "es", "fr", "de", "it", "pt", "pl", "tr", "ja", "zh", "ko"]
SPEEDS = ["battery", "balanced", "turbo"]


def read_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"enabled": False, "status": "off", "detail": "service not running"}


def send(line: str) -> None:
    # O_NONBLOCK: if the service is not reading, open() fails with ENXIO
    # instead of blocking forever.
    try:
        fd = os.open(FIFO_FILE, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        print("livesubtitles: service not running "
              "(systemctl --user start livesubtitles)", file=sys.stderr)
        raise SystemExit(1)
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    cmd = args[0]
    if cmd == "status":
        print(json.dumps(read_state(), ensure_ascii=False))
        return 0
    if cmd == "lang":
        if len(args) < 2 or args[1] not in LANGUAGES:
            print("languages: " + " ".join(LANGUAGES), file=sys.stderr)
            return 2
        send(" ".join(args))
        return 0
    if cmd in ("toggle", "enable", "disable"):
        send(cmd)
        return 0
    if cmd == "source":
        send(" ".join(args))
        return 0
    if cmd == "original":
        arg = args[1] if len(args) > 1 else "on"
        if arg not in ("on", "off"):
            print("usage: livesubtitles-cli original on|off", file=sys.stderr)
            return 2
        send(f"original {arg}")
        return 0
    if cmd == "speed":
        if len(args) < 2 or args[1] not in SPEEDS:
            print("speed profiles: " + " ".join(SPEEDS), file=sys.stderr)
            return 2
        send(" ".join(args))
        return 0
    if cmd == "apps":
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
        try:
            from livesubtitles.service import list_audio_apps
        except Exception as e:
            print(f"livesubtitles: {e}", file=sys.stderr)
            return 1
        apps = list_audio_apps()
        if not apps:
            print("no apps playing audio right now")
        for a in apps:
            print(f"{a['id']}\t{a['name']}")
        return 0
    if cmd == "capture":
        if len(args) < 2 or not (args[1] == "system" or args[1].startswith("si")):
            print("usage: livesubtitles-cli capture system|si<N> "
                  "(see: livesubtitles-cli apps)", file=sys.stderr)
            return 2
        send(" ".join(args))
        return 0
    if cmd == "opacity":
        try:
            value = float(args[1])
            if not 0.2 <= value <= 1.0:
                raise ValueError
        except (IndexError, ValueError):
            print("usage: livesubtitles-cli opacity <0.2-1.0>", file=sys.stderr)
            return 2
        send(f"opacity {value}")
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
