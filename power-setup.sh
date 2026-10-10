#!/bin/bash
# power-setup.sh — install the battery guard (power_guard.py) for a Pi with a
# Waveshare UPS HAT (C).
#
# Run on the Pi with sudo, after the UPS is fitted and I2C is enabled:
#
#   sudo ./power-setup.sh
#
# It checks the UPS can be read, installs the riko-power service, and restarts
# the monitor and the display so they pick up the battery alerts. Safe to re-run.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
RIKO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$RIKO_DIR"

echo "[1/3] reading the UPS"
.venv/bin/python3 power_guard.py --check

echo "[2/3] riko-power service"
cat > /etc/systemd/system/riko-power.service <<EOF
[Unit]
Description=Riko Pi battery guard
After=multi-user.target

[Service]
User=root
WorkingDirectory=$RIKO_DIR
EnvironmentFile=$RIKO_DIR/.env
ExecStart=$RIKO_DIR/.venv/bin/python3 power_guard.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable riko-power
systemctl restart riko-power

echo "[3/3] restarting the monitor and the display"
systemctl restart riko-monitor riko-oled 2>/dev/null || true
echo "done — follow it with: journalctl -u riko-power -f"
