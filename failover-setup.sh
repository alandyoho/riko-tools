#!/bin/bash
# failover-setup.sh — install the Wi-Fi → cellular failover (wifi_failover.py).
#
# Run on the Pi with sudo, after the cellular connection works
# (`nmcli connection up cellular-1nce` gets an address and can ping):
#
#   sudo ./failover-setup.sh <feeder-mac> [admin-mac ...]
#
# feeder-mac  the only device that gets internet through the hotspot
# admin-mac   optional: devices that may also join the hotspot, to SSH into the
#             Pi during a takeover (they get an address, but no internet)
#
# Every other device is refused by the hotspot, even with the right password.
#
# It installs hostapd, writes /etc/riko-failover/ (a copy of the home Wi-Fi's
# name and password, the MAC allowlist, DHCP settings) and installs the
# riko-failover service. Nothing goes on air until a takeover. Safe to re-run.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
[[ $# -ge 1 ]] || { echo "usage: sudo $0 <feeder-mac> [admin-mac ...]" >&2; exit 1; }

RIKO_DIR="$(cd "$(dirname "$0")" && pwd)"
HOME_CON="${RIKO_HOME_WIFI_CON:-netplan-wlan0-LFYTT-iot}"
CHANNEL="${RIKO_FAILOVER_CHANNEL:-6}"
COUNTRY="${RIKO_FAILOVER_COUNTRY:-US}"
AP_DIR=/etc/riko-failover
MACS=("${@,,}")
FEEDER_MAC="${MACS[0]}"
for mac in "${MACS[@]}"; do
    [[ $mac =~ ^([0-9a-f]{2}:){5}[0-9a-f]{2}$ ]] || { echo "not a MAC address: $mac" >&2; exit 1; }
done

SSID="$(nmcli -g 802-11-wireless.ssid connection show "$HOME_CON")"
PSK="$(nmcli -s -g 802-11-wireless-security.psk connection show "$HOME_CON")"
[[ -n $SSID && -n $PSK ]] || { echo "couldn't read SSID/password from $HOME_CON" >&2; exit 1; }

echo "[1/4] hostapd"
if ! command -v hostapd >/dev/null; then
    apt-get install -y hostapd
fi
# we start hostapd ourselves during a takeover; the packaged service must stay off
systemctl disable --now hostapd >/dev/null 2>&1 || true

echo "[2/4] $AP_DIR (SSID $SSID, channel $CHANNEL, ${#MACS[@]} allowed device(s))"
install -d -m 700 "$AP_DIR"
printf '%s\n' "${MACS[@]}" > "$AP_DIR/accept"
if [[ $PSK =~ ^[0-9a-fA-F]{64}$ ]]; then KEYLINE="wpa_psk=$PSK"; else KEYLINE="wpa_passphrase=$PSK"; fi
cat > "$AP_DIR/hostapd.conf" <<EOF
# written by failover-setup.sh
interface=wlan0
driver=nl80211
ssid=$SSID
country_code=$COUNTRY
hw_mode=g
channel=$CHANNEL
ieee80211n=1
wmm_enabled=1
auth_algs=1
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
$KEYLINE
macaddr_acl=1
accept_mac_file=$AP_DIR/accept
EOF
chmod 600 "$AP_DIR/hostapd.conf"
cat > "$AP_DIR/dnsmasq.conf" <<EOF
# written by failover-setup.sh
interface=wlan0
bind-interfaces
except-interface=lo
dhcp-range=10.42.0.10,10.42.0.50,255.255.255.0,12h
dhcp-host=$FEEDER_MAC,10.42.0.77
dhcp-option=option:router,10.42.0.1
dhcp-option=option:dns-server,10.42.0.1
pid-file=/run/riko-failover-dnsmasq.pid
EOF

echo "[3/4] riko-failover service"
cat > /etc/systemd/system/riko-failover.service <<EOF
[Unit]
Description=Riko Wi-Fi to cellular failover
After=NetworkManager.service ModemManager.service
Wants=ModemManager.service

[Service]
User=root
WorkingDirectory=$RIKO_DIR
EnvironmentFile=$RIKO_DIR/.env
Environment=RIKO_FEEDER_MAC=$FEEDER_MAC
Environment=RIKO_HOME_WIFI_CON=$HOME_CON
ExecStart=$RIKO_DIR/.venv/bin/python3 wifi_failover.py
ExecStopPost=$RIKO_DIR/.venv/bin/python3 wifi_failover.py --cleanup
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

echo "[4/4] preflight"
cd "$RIKO_DIR"
RIKO_FEEDER_MAC="$FEEDER_MAC" RIKO_HOME_WIFI_CON="$HOME_CON" .venv/bin/python3 wifi_failover.py --check

systemctl enable riko-failover
systemctl restart riko-failover
echo "done — follow it with: journalctl -u riko-failover -f"
