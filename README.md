# riko-tools

Unofficial tools and findings for the **Neakasa Riko** fresh-made wet-meal cat feeder.

The Riko is a good machine let down by its software. This repo is the result of a few
days of reading what the feeder actually reports to the cloud (rather than what the app
shows), documenting the bugs, and building tools to work around them. Everything here
talks to the same cloud API the official app uses; nothing modifies the device firmware.

**Not affiliated with Neakasa.** Uses app keys published in the open-source
[neakasa-litterbox-sdk](https://pypi.org/project/neakasa-litterbox-sdk/). It works today;
a Neakasa backend change could break it at any time.

---

## The one thing most owners want: fix the bowl weight

**The problem:** the Riko ships believing its food bowl weighs 65 g. The bowls in the box
actually weigh about 68 g. The feeder computes "food left in the bowl" as (scale reading −
stored bowl weight), so every reading is off by ~3 g — it reports food left in a bowl
you just washed, and its "how much did the cat eat" numbers are wrong by the same amount.
**The app's tare button does not fix this** — it zeroes the platform but never updates the
stored 65 g. There is no bowl-weight setting anywhere in the app.

**The fix:** a small script sends the correct weight to the feeder directly.

### Never used a "terminal" before? Start here.

You don't need to know anything technical. Follow these exactly. This is written for a
**Mac** — the most common case. (On Windows this needs extra setup; skip to the Python
section or ask someone technical.)

**1. Weigh your empty bowl.** Use a kitchen scale. Most are ~68 g. Write down your number
— you'll type it in later. If you don't have a scale, 68 is a safe default.

**2. Download the script.** At the top of this page, click the green **`Code`** button →
**Download ZIP**. Open your Downloads folder and double-click the ZIP to unzip it. You'll
get a folder called `riko-tools-main`.

**3. Open Terminal.** Press `Cmd + Space`, type `Terminal`, press Enter. A window with a
text prompt appears. This is where you type commands. Don't worry — you'll only type two.

**4. Go to the folder.** Type this, then a space, then **drag the `riko-tools-main` folder
from Finder into the Terminal window** (it pastes the location for you), then press Enter:

```
cd 
```

(So it reads `cd /Users/you/Downloads/riko-tools-main` — the `cd` means "go to".)

**5. Run the fix.** Type this and press Enter:

```
bash fix_bowl_weight.sh
```

That's the whole command — no email, no weight, nothing to remember. The script then
**walks you through everything, one step at a time:**

- Asks for your **Neakasa email and password**. The password is hidden as you type
  (that's normal) and is only ever sent to Neakasa to log in — never saved.
- The first time, if a tool called `jq` is missing, it offers to install it — type `y`.
  (If that install asks for your **Mac** password, that's your computer's login password,
  not your Neakasa one.)
- If you have **more than one feeder**, it lists them and lets you pick with the arrow keys.
- It **weighs your bowl for you** using the feeder's own scale — no kitchen scale needed.
  It'll ask you to put an empty bowl on the tray (and to lift it off and back on, so the
  feeder takes a fresh reading). If you have several bowls, it can measure them all and
  average them.
- It shows you what's wrong and asks you to confirm before changing anything.
- It tells you to take the bowl **off** to apply, then put it **back** to verify, and
  confirms the fix worked.

That's it — just answer the prompts. Nothing is destructive; if anything looks off you
can re-run it any time.

### Already comfortable with a terminal?

```bash
./fix_bowl_weight.sh                       # guided: prompts for everything
./fix_bowl_weight.sh -e you@x.com -w 68    # or pass values directly to skip the prompts
```

Run with no arguments for the guided flow (prompts for email/password, picks the device,
measures the bowl via the feeder's scale, confirms). Or pass flags to script it:
`-e email`, `-w grams`, `-d device_name` (for multiple feeders), `-n` dry-run, `-v`
verbose. macOS or Linux; needs `curl`, `openssl`, `jq`, `xxd` (it offers to install any
that are missing). Windows: use WSL or Git Bash.

### Prefer Python (works on Windows too)

```bash
pip install neakasa-litterbox-sdk
python3 fix_bowl_weight.py --email you@example.com --weight 68
```

Same fix via Python if you're on Windows or would rather not use bash. (The rich guided
walkthrough — device picker, self-measuring, multi-bowl averaging — is in the bash
version; the Python one takes the values as flags.)

### For everyone

**Your Neakasa password** is only used to log in, exactly as the app does, and is never
saved anywhere. The full source is in this repo — read it, or have someone read it, before
running it. It only ever contacts Neakasa's own servers.

**One catch:** the Neakasa app seems to reset this correction back to the wrong value (65)
when it updates. If your readings drift back to showing phantom food, just run the fix
again.

---

## What else is here

| File | What it does |
|---|---|
| `fix_bowl_weight.sh` / `.py` | The bowl-weight fix above. |
| `riko.py` | Full command-line control of the feeder: status, feed now, edit the schedule, set the tare, set the timezone, decode errors, recover a stalled pump. |
| `monitor.py` + `notify.py` + `config.py` | A background monitor (systemd-friendly) that watches for missed feeds, pump stalls, clock drift, low food/water and **setting changes**, fixes the safe cases under hard caps, and pushes phone notifications via [ntfy](https://ntfy.sh). |
| `neakasa.py` | Reads the intake ledger — per-meal actual-vs-planned grams, failure reasons, and eat sessions — from Neakasa's feeder backend. Self-sufficient: logs in with your account and needs the feeder owner's user_id (a number, not a secret). See "Intake history" below. |
| `setup.sh` | Guided setup for the monitor — see below. Start here if you want more than the bowl-weight fix. |
| `riko_discover.py`, `riko_tsl_probe.py`, `riko_trace.py`, `riko_poll.py`, `riko_pump_test.py` | Diagnostic scripts used to produce the findings. |

`riko.py --help` and each script's header explain usage. Most read credentials from a
`riko.toml` (see `config.py --example`) or `RIKO_EMAIL` / `RIKO_PASSWORD` env vars.

---

## Running the monitor

The bowl-weight fix above is a one-off script. If you want ongoing monitoring — push
notifications when a feed fails, a pump stalls, or something's wrong, with a few known
issues fixed automatically — there's a guided setup for that too:

```bash
git clone https://github.com/<you>/riko-tools.git
cd riko-tools
./setup.sh
```

Same idea as `fix_bowl_weight.sh`: no arguments, no prior setup, just answer the
prompts. It walks through:

1. Creating a Python virtual environment and installing dependencies
2. Your Neakasa login (stored locally in a `.env` file, same handling as the bowl fix)
3. Finding your feeder on the account (auto-picks if you only have one)
4. Writing a starter `riko.toml`
5. A live test to confirm it can see your feeder
6. **Optional:** installing it as a background service (`systemd`) that runs continuously,
   with push notifications via [ntfy](https://ntfy.sh) — free, no account needed, you
   just pick a topic name and subscribe to it in the ntfy app

It's written for a Raspberry Pi running Raspberry Pi OS, but works on any Linux box with
`systemd`. Safe to re-run — every step checks whether it's already done first.

**The background service starts in `--dry-run` mode on purpose** — it'll detect problems
and notify you, but won't change anything on the device until you turn that off. Give it
a day or two to make sure it's telling you the right things, then remove `--dry-run` from
the service file it created (`/etc/systemd/system/riko-monitor.service`) and restart it.

If you'd rather set things up by hand instead of running `setup.sh`, `config.py` and the
file table above have what you need — the script is just a shortcut through the same
steps.

---

## Intake history (what the cat actually ate)

The feeder logs every meal to Neakasa's backend: planned vs actually-dispensed food
and water, failure reasons, and post-meal "eat sessions" (how much was eaten). The app
shows a sliver of this; `neakasa.py` pulls the lot.

```bash
python3 neakasa.py intake --days 14      # summary: delivered vs planned, failure counts
python3 neakasa.py ledger --days 7       # per-meal table + eat sessions
python3 neakasa.py raw --days 2          # raw JSON
```

It authenticates with your normal `[account]` credentials. One wrinkle worth knowing:
the ledger is keyed off the feeder **owner's** user_id, which can differ from the
account you automate with if the device was *shared* to your automation account. A
shared account can still read the owner's records by passing the owner's user_id —
set `feeder_owner_id` under `[feeder]` in your config (it's an account number, not a
secret), or pass `--owner-id`. If you automate with the same account that owns the
feeder, you don't need it at all.

To find the owner's user_id: log in once as the owning account and print `ali_user_id`,
or capture it from the app. It never changes.

## Findings

Written up as daily reviews as the investigation went. Short version:

- **Daylight saving time was ignored** (fixed by Neakasa in firmware 1.0.0-0023). Before
  that, every scheduled meal fired an hour late and the app marked it "Expired."
- **The 1.0.0-0023 update broke the rehydration soak.** Scheduled meals now serve
  immediately after grinding with no soak, and the soak setting is ignored. This is the
  feeder's core function; details in the day-4/day-5 writeups.
- **The bowl-weight default is wrong** (above), and the app re-breaks it after updates.
- **The food-level sensor false-alarms** "low food" on a full bin.
- **Battery/scheduled feeds are unreliable.** On battery the feeder sleeps and sometimes
  wakes near a slot but declines to feed; manual feeds on battery work fine. A power cut
  can mean missed meals with no notification (in a real outage your wifi is down too).
- **Intake data lives only on Neakasa's backend** and never crosses the device channel,
  so the device-control API can't see what the cat actually ate.

See the `*-review.md` and `riko-findings.md` files for the full, evidence-backed detail.

---

## Security notes

- Your account password is only ever sent to Neakasa's login endpoint, the same as the
  app. It is not stored by these tools. Read the source and confirm that for yourself —
  that's the whole point of this being open.
- The app keys/secrets in the scripts are **not personal** — they're the app's own,
  already published in the SDK linked above.
- If you fork or share captures, note that a full property dump from the device includes
  its cloud credentials, MAC and your public IP; keep those private.

## Disclaimer

Unofficial, unsupported, provided as-is. This talks to a live cloud service using an
unofficial method and could stop working or behave unexpectedly if Neakasa changes their
backend. It does not touch device firmware. Use on your own account and your own device.
If something here helps Neakasa fix the underlying bugs, all the better — that's the goal.
