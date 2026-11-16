#!/usr/bin/env bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
export DATA_DIR="${DATA_DIR:-/app/data}"
mkdir -p "$DATA_DIR/chrome-profiles" "$DATA_DIR/chrome-profile"

# Stale Chromium locks survive volume remounts after deploy/restart.
clear_chrome_locks() {
  local dir="$1"
  [[ -d "$dir" ]] || return 0
  rm -f "$dir/SingletonLock" "$dir/SingletonCookie" "$dir/SingletonSocket" 2>/dev/null || true
}
clear_chrome_locks "$DATA_DIR/chrome-profile"
find "$DATA_DIR/chrome-profiles" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | while read -r profile; do
  clear_chrome_locks "$profile"
done
echo "Cleared stale Chromium profile locks under $DATA_DIR"

if [[ "${ENABLE_VNC:-1}" == "1" ]]; then
  echo "Starting Xvfb on $DISPLAY"
  Xvfb "$DISPLAY" -screen 0 1365x900x24 -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
  sleep 1
  fluxbox >/tmp/fluxbox.log 2>&1 &
  x11vnc -display "$DISPLAY" -forever -shared -rfbport 5900 -nopw -xkb >/tmp/x11vnc.log 2>&1 &
  # Root URL should open the client, not a directory listing.
  ln -sfn /usr/share/novnc/vnc.html /usr/share/novnc/index.html
  websockify --web /usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 &
  echo "noVNC ready on :6080  (for Chromium login)"
fi

exec python run.py --host 0.0.0.0 --port "${PORT:-8000}" --no-browser
