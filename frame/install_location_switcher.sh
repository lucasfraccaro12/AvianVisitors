#!/bin/bash
# Start the bird frame's location switcher on boot, at http://<this-pi>.local:8080
# Run from the AvianVisitors/frame folder, as your normal user (not with sudo):
#   ./install_location_switcher.sh
set -euo pipefail
FRAME="$(cd "$(dirname "$0")" && pwd)"
PORT="${1:-8080}"
[ "$(id -u)" != 0 ] || { echo "Run this as your normal user, without sudo." >&2; exit 1; }
[ -f "$FRAME/location_switcher.py" ] || { echo "location_switcher.py isn't next to this script." >&2; exit 1; }
[ -x "$FRAME/.venv/bin/python" ] || { echo "Run the frame's install.sh first (no .venv found)." >&2; exit 1; }
case "$PORT" in ''|*[!0-9]*) echo "Port must be a number." >&2; exit 1 ;; esac

sudo tee /etc/systemd/system/birdframe-locations.service >/dev/null <<UNIT
[Unit]
Description=AvianVisitors frame location switcher (web page)
Wants=network-online.target
After=network-online.target

[Service]
User=$USER
WorkingDirectory=$FRAME
ExecStart=$FRAME/.venv/bin/python $FRAME/location_switcher.py --port $PORT
Restart=on-failure
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now birdframe-locations.service
echo
echo "Done. On your phone (same Wi-Fi), open:  http://$(hostname).local:$PORT"
echo "Then use Share > Add to Home Screen to keep it as an app icon."
