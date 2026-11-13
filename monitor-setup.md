# Setting up the Riko monitor

The monitor is `monitor.py` + `notify.py`, built on `riko.py`. It detects problems,
fixes the safe ones under hard caps, and pushes notifications to your phone. It uses
only the Aliyun channel, so it needs nothing beyond your existing setup.

## What it does

Detects: missed feeds, pump stalls (code 70), other error codes, clock drift, device
offline, low food/water.

Auto-fixes (only these, only under caps): pump stalls via `unclog`, and clock drift
by re-applying your tz offset. Everything else is notify-only. Grinder faults are
never auto-retried.

Safety caps: one unclog per slot per day, a daily remediation cap, a hard daily
gram ceiling (won't feed past it — escalates to you instead), refuses to act from an
unknown state, and a kill-switch file.

## 1. Get the code (and update it)

`~/riko` on the Pi is a plain checkout of this repo:

    git clone https://github.com/alandyoho/riko-tools.git ~/riko

To update it, push to GitHub, then on the Pi run `~/riko/deploy.sh` (from the Mac:
`ssh yoho@<pi> riko/deploy.sh`). It fetches origin/main, runs every test in a scratch
copy, and only if they all pass moves `~/riko` forward; then it says whether the monitor
needs a restart. Don't copy files over by hand - local edits make it refuse to deploy.

## 2. Set up phone notifications (ntfy — no account needed)

Install the **ntfy** app (iOS/Android). Pick an unguessable topic name, e.g.
`riko-mixtape-7f3k9`. Subscribe to it in the app.

Add to `~/riko/riko.toml` on the Pi:

    [notify]
    ntfy_topic = "riko-mixtape-7f3k9"
    # ntfy_server = "https://ntfy.sh"   # default; self-host if you prefer

(Or export `RIKO_NTFY_TOPIC` in the `.env`.) Anyone who knows the topic can read
your alerts, so keep the name private — that's the tradeoff for zero-setup.

`config.py` already reads the `[notify]` table, so no patching needed. Leaving the
topic unset simply disables push (everything still logs to the journal).

## 3. Test it safely first

    # one pass, no changes, just see what it detects and sends
    ssh -t yoho@192.168.1.151 'cd riko && source .venv/bin/activate && \
        set -a && source .env && set +a && python3 monitor.py --once --dry-run'

You should get a test-worthy notification only if something's actually wrong. Force a
test push by temporarily setting a wrong tz and running --once --dry-run (it will
report "Clock drifted" without fixing it).

## 4. Run it for real, as a service

Create `/etc/systemd/system/riko-monitor.service`:

    [Unit]
    Description=Riko monitor
    After=network-online.target riko-watch.service
    Wants=network-online.target

    [Service]
    User=yoho
    WorkingDirectory=/home/yoho/riko
    EnvironmentFile=/home/yoho/riko/.env
    ExecStart=/home/yoho/riko/.venv/bin/python3 monitor.py --interval 30
    Restart=always
    RestartSec=20

    [Install]
    WantedBy=multi-user.target

Then:

    sudo systemctl daemon-reload && sudo systemctl enable --now riko-monitor
    journalctl -u riko-monitor -f

Also turn off Wi-Fi power saving on the Pi. With it on, the Pi drops off the network
intermittently (SSH gets "No route to host"), and a drop in the middle of a login can
fail it partway. This persists across reboots (NetworkManager) and applies now (iw):

    printf '[connection]\nwifi.powersave = 2\n' | sudo tee /etc/NetworkManager/conf.d/wifi-powersave-off.conf
    sudo /sbin/iw dev wlan0 set power_save off
    /sbin/iw dev wlan0 get power_save    # should say: Power save: off

## 5. Start conservative

Run it `--dry-run` (edit the ExecStart to add the flag) for a day or two first, so
you can watch what it *would* do without it acting. Once the pump-stall handling has
fired correctly at least once and you trust it, remove `--dry-run`.

## Controls

- **Stop all auto-fixing immediately:** `touch ~/riko/riko_state/DISABLE_REMEDIATION`
  Detection and notifications continue; nothing is changed on the device. Delete the
  file to re-enable.
- **Tune the caps:** they're in the `Policy` dataclass in `monitor.py` — daily gram
  ceiling, max remediations/day, missed-feed grace, offline threshold.

## What it deliberately can't do

- **Track actual intake / "did the cat eat".** That data lives only on Neakasa's app
  backend behind an encrypted token (see key-extraction-plan.md). The monitor tracks
  PLANNED grams and says so.
- **Retract the bowl when empty.** Same reason — no reliable empty-detection without
  the ledger.

Both are honest limitations of where our access is, not bugs.
