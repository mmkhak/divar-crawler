#!/usr/bin/env bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
export DATA_DIR="${DATA_DIR:-/app/data}"
mkdir -p "$DATA_DIR/chrome-profile"

if [[ "${ENABLE_VNC:-1}" == "1" ]]; then
  echo "Starting Xvfb on $DISPLAY"
  Xvfb "$DISPLAY" -screen 0 1365x900x24 -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
  sleep 1
  fluxbox >/tmp/fluxbox.log 2>&1 &
  x11vnc -display "$DISPLAY" -forever -shared -rfbport 5900 -nopw -xkb >/tmp/x11vnc.log 2>&1 &
  websockify --web /usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 &
  echo "noVNC ready on :6080  (for Chromium login)"
fi

exec python run.py --host 0.0.0.0 --port "${PORT:-8000}" --no-browser
