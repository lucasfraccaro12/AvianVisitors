#!/bin/bash
# Check for rare birds every 10 minutes and alert your phones through ntfy.
# Run from the AvianVisitors/frame folder, as your normal user (not with sudo):
#   ./install_rare_alerts.sh
set -euo pipefail
FRAME="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" != 0 ] || { echo "Run this as your normal user, without sudo." >&2; exit 1; }
[ -f "$FRAME/rare_bird_alerts.py" ] || { echo "rare_bird_alerts.py isn't next to this script." >&2; exit 1; }
[ -x "$FRAME/.venv/bin/python" ] || { echo "Run the frame's install.sh first (no .venv found)." >&2; exit 1; }

"$FRAME/.venv/bin/python" "$FRAME/rare_bird_alerts.py" --init

sudo tee /etc/systemd/system/birdframe-alerts.service >/dev/null <<UNIT
[Unit]
Description=AvianVisitors rare bird alerts (one check)
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$FRAME
ExecStart=$FRAME/.venv/bin/python $FRAME/rare_bird_alerts.py
TimeoutStartSec=120
NoNewPrivileges=true
UNIT

sudo tee /etc/systemd/system/birdframe-alerts.timer >/dev/null <<UNIT
[Unit]
Description=Check for rare birds every 10 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=10min
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now birdframe-alerts.timer

echo
read -r -p "Subscribed on both phones? Press Enter to send a test notification... " _
"$FRAME/.venv/bin/python" "$FRAME/rare_bird_alerts.py" --test
echo
echo "Done. Settings: ~/.birdframe/alerts.toml   Log: journalctl -u birdframe-alerts"
