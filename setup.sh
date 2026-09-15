#!/usr/bin/env bash
#
# setup.sh — guided setup for the Riko monitoring stack.
#
# Gets a fresh checkout of this repo running as a background service that
# watches your Neakasa Riko feeder and can text/push you when something's
# wrong (and auto-fixes a few known issues). Written for a Raspberry Pi
# running Raspberry Pi OS / Debian, but works on any systemd Linux box.
#
# USAGE
#   ./setup.sh
#
# Run it from inside the cloned repo. It's interactive and safe to re-run —
# every step checks whether it's already done before doing it again.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

# ---- visual helpers, matching fix_bowl_weight.sh's style ----------------------
if [[ -t 1 ]]; then
  C_BANNER=$'\033[1;44;97m'; C_ACT=$'\033[1;92m'; C_HDR=$'\033[1;96m'; C_OFF=$'\033[0m'
else
  C_BANNER="" C_ACT="" C_HDR="" C_OFF=""
fi
banner(){ echo; echo "${C_BANNER} $* ${C_OFF}"; }
act(){    echo "${C_ACT}➤ $*${C_OFF}"; }
ask(){    read -rp "${C_ACT}➤ $1${C_OFF}" "$2"; }

echo
echo "${C_BANNER} Riko monitor — guided setup ${C_OFF}"
echo
echo "This sets up:"
echo "  1. A Python virtual environment with everything installed"
echo "  2. Your Neakasa login, stored locally (never sent anywhere but Neakasa)"
echo "  3. A quick test that it can actually see your feeder"
echo "  4. Optional: a background service that watches it continuously and"
echo "     can text/push you when something's wrong"
echo
echo "Safe to re-run — it skips anything already done."
echo

# ============================================================================
# STEP 1 — dependencies
# ============================================================================
banner "STEP 1 of 5 — Python environment"

if ! command -v python3 >/dev/null; then
  echo "python3 isn't installed. On a Raspberry Pi / Debian:" >&2
  echo "  sudo apt update && sudo apt install -y python3 python3-venv python3-pip" >&2
  exit 1
fi
PYVER=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
echo "found python3 ($PYVER)"

if [[ ! -d .venv ]]; then
  act "Creating a virtual environment (.venv)…"
  python3 -m venv .venv
else
  echo "  .venv already exists, reusing it"
fi

source .venv/bin/activate
act "Installing dependencies (this can take a minute on a Pi)…"
pip install --quiet --upgrade pip
if [[ -f requirements.txt ]]; then
  pip install --quiet -r requirements.txt
else
  pip install --quiet neakasa-litterbox-sdk aiohttp
fi
echo "  done."

# ============================================================================
# STEP 2 — credentials
# ============================================================================
banner "STEP 2 of 5 — your Neakasa account"

if [[ -f .env ]] && grep -q "NEAKASA_EMAIL=" .env 2>/dev/null; then
  EXISTING_EMAIL=$(grep "NEAKASA_EMAIL=" .env | head -1 | cut -d= -f2-)
  echo "Found existing credentials for: $EXISTING_EMAIL"
  ask "Keep these? [Y/n] " keep
  if [[ ${keep:-Y} =~ ^[Yy] ]]; then
    SKIP_CREDS=1
  fi
fi

if [[ -z ${SKIP_CREDS:-} ]]; then
  echo
  echo "${C_HDR}This is the same login you use in the Neakasa app.${C_OFF}"
  echo "It's used only to talk to Neakasa's own servers — logging in and reading"
  echo "your device, the same as the app does. It's stored in a local .env file"
  echo "(not in this repo, not sent anywhere else, permissions locked to your user)."
  echo
  ask "Neakasa email: " EMAIL
  read -rsp "${C_ACT}➤ Neakasa password: ${C_OFF}" PASSWORD; echo

  cat > .env <<EOF
NEAKASA_EMAIL=$EMAIL
NEAKASA_PASSWORD=$PASSWORD
EOF
  chmod 600 .env
  echo "  saved to .env (permissions locked to your user only)"
fi

set -a; source .env; set +a

# ============================================================================
# STEP 3 — find your device
# ============================================================================
banner "STEP 3 of 5 — finding your feeder"

act "Connecting and listing devices on this account…"
DEVICE_LIST=$(python3 - <<'PYEOF'
import asyncio, os, sys
try:
    from riko import Riko
except ImportError:
    print("IMPORT_ERROR", file=sys.stderr); sys.exit(1)

async def m():
    async with Riko(os.environ["NEAKASA_EMAIL"], os.environ["NEAKASA_PASSWORD"]) as r:
        devices = await r._client.list_devices()
        for d in devices:
            print(f"{d.device_name}\t{d.product_name}")
asyncio.run(m())
PYEOF
) || { echo "Couldn't connect. Check your email/password and try again." >&2
       rm -f .env; exit 1; }

if [[ -z "$DEVICE_LIST" ]]; then
  echo "No devices found on this account. Make sure the app shows your feeder" >&2
  echo "under this login before continuing." >&2
  exit 1
fi

DEVICE_COUNT=$(echo "$DEVICE_LIST" | wc -l | tr -d ' ')
if [[ $DEVICE_COUNT -eq 1 ]]; then
  DEVICE_NAME=$(echo "$DEVICE_LIST" | cut -f1)
  echo "Found: $DEVICE_NAME"
else
  echo "Found $DEVICE_COUNT devices:"
  echo "$DEVICE_LIST" | nl -w2 -s') '
  ask "Which one is the feeder to monitor? [1-$DEVICE_COUNT]: " pick
  DEVICE_NAME=$(echo "$DEVICE_LIST" | sed -n "${pick}p" | cut -f1)
fi
echo "  using device: $DEVICE_NAME"

# ============================================================================
# STEP 4 — write config
# ============================================================================
banner "STEP 4 of 5 — configuration"

if [[ -f riko.toml ]]; then
  echo "riko.toml already exists — leaving it as-is."
  echo "(delete it and re-run this script if you want a fresh one)"
else
  cat > riko.toml <<EOF
[account]
# credentials come from .env — leave this section commented
# email = "you@example.com"
# password = "..."
region = "US"

[device]
device_name = "$DEVICE_NAME"
bowl_grams = 68        # correct empty-bowl weight — see the README if yours differs
tz_offset = -5

[runtime]
poll_seconds = 30

[feeder]
# feeder_owner_id = 400133257   # only needed if your automation account is
                                 # different from the one that owns the feeder
                                 # in the phone app — see README "Intake history"
EOF
  echo "  wrote riko.toml"
fi

# ============================================================================
# STEP 5 — test, then offer the background service
# ============================================================================
banner "STEP 5 of 5 — test & (optional) background service"

act "Testing a live status read…"
python3 riko.py status || { echo "Status check failed — see the error above." >&2; exit 1; }

echo
echo "${C_ACT}✓ Working.${C_OFF} You can now run things like:"
echo "    python3 riko.py status"
echo "    python3 riko.py feed 8 24"
echo "    python3 neakasa.py intake --days 7"
echo

ask "Set up the background monitor now? It watches continuously and can text/push you and auto-fix a few known issues. [Y/n] " setup_service
if [[ ! ${setup_service:-Y} =~ ^[Yy] ]]; then
  echo "Skipping. Run this script again any time to set it up later."
  exit 0
fi

echo
echo "${C_HDR}Notifications go through ntfy (ntfy.sh) — a free push-notification"
echo "service. You'll pick a topic name (like a channel name) and subscribe to"
echo "it in the ntfy app on your phone.${C_OFF}"
echo
echo "Pick a topic name (make it hard to guess — anyone who knows it can read"
echo "your notifications). Leave blank for a random one."
ask "Topic name: " NTFY_TOPIC
if [[ -z "$NTFY_TOPIC" ]]; then
  NTFY_TOPIC="riko-$(head -c4 /dev/urandom | xxd -p)"
  echo "  (using a random one: $NTFY_TOPIC)"
fi

cat >> .env <<EOF
NTFY_TOPIC=$NTFY_TOPIC
EOF
echo
echo "Subscribe to this in the ntfy app (iOS/Android) or at https://ntfy.sh/$NTFY_TOPIC :"
echo "  ${C_ACT}$NTFY_TOPIC${C_OFF}"
echo

USER_NAME=$(whoami)
SERVICE_DIR="/etc/systemd/system"
if [[ ! -w $SERVICE_DIR ]] && ! sudo -n true 2>/dev/null; then
  echo "Installing the background service needs sudo. You may be prompted for"
  echo "your password."
fi

WATCH_UNIT="$SERVICE_DIR/riko-watch.service"
MONITOR_UNIT="$SERVICE_DIR/riko-monitor.service"

sudo tee "$WATCH_UNIT" > /dev/null <<EOF
[Unit]
Description=Riko discovery watcher
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$REPO_DIR
EnvironmentFile=$REPO_DIR/.env
ExecStart=$REPO_DIR/.venv/bin/python3 riko_discover.py --poll 15
Restart=always
RestartSec=20

[Install]
WantedBy=multi-user.target
EOF

sudo tee "$MONITOR_UNIT" > /dev/null <<EOF
[Unit]
Description=Riko monitor
After=network-online.target riko-watch.service
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$REPO_DIR
EnvironmentFile=$REPO_DIR/.env
ExecStart=$REPO_DIR/.venv/bin/python3 monitor.py --interval 30 --dry-run
Restart=always
RestartSec=20

[Install]
WantedBy=multi-user.target
EOF

echo "  wrote $WATCH_UNIT"
echo "  wrote $MONITOR_UNIT"
echo
echo "${C_HDR}Starting in --dry-run mode on purpose: it'll detect and notify you"
echo "about issues, but won't automatically change anything on the device yet.${C_OFF}"
echo "Once you've watched it for a day or two and trust it, remove --dry-run"
echo "from $MONITOR_UNIT and run: sudo systemctl daemon-reload && sudo systemctl restart riko-monitor"
echo

sudo systemctl daemon-reload
sudo systemctl enable --now riko-watch.service
sudo systemctl enable --now riko-monitor.service

sleep 3
echo
echo "${C_ACT}✓ Running.${C_OFF} Check on it any time with:"
echo "    sudo systemctl status riko-monitor"
echo "    journalctl -u riko-monitor -f"
echo
echo "You're set up. See the README for what the monitor watches for, and"
echo "fix_bowl_weight.sh if your feeder's food-remaining readings look wrong."
