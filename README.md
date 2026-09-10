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

**The fix:** send the correct weight directly. Two versions, same result.

### Bash (recommended — no Python needed)

```bash
# weigh your empty bowl first, then:
./fix_bowl_weight.sh -e you@example.com -w 68
```

Runs on **macOS or Linux**. Needs `curl`, `openssl`, `jq`, `xxd` — the script checks for
them on first run and offers to install anything missing with your package manager
(it asks first). It walks you through taking the bowl out, sets the value, has you put
the bowl back, and confirms an empty bowl now reads ~0 g.

*(Windows: use WSL or Git Bash.)*

### Python

```bash
pip install neakasa-litterbox-sdk
python3 fix_bowl_weight.py --email you@example.com --weight 68
```

Same thing, if you'd rather use Python or are on Windows.

**Both ask for your Neakasa password** — it's used only to log in, exactly as the app does,
and is never stored. The source is right here; read it before you run it.

**Caveat:** the Neakasa app appears to overwrite this correction with its own default (65)
when it syncs, seen after an app update. If your readings drift back, re-run the fix.

---

## What else is here

| File | What it does |
|---|---|
| `fix_bowl_weight.sh` / `.py` | The bowl-weight fix above. |
| `riko.py` | Full command-line control of the feeder: status, feed now, edit the schedule, set the tare, set the timezone, decode errors, recover a stalled pump. |
| `monitor.py` + `notify.py` + `config.py` | A background monitor (systemd-friendly) that watches for missed feeds, pump stalls, clock drift, low food/water and **setting changes**, fixes the safe cases under hard caps, and pushes phone notifications via [ntfy](https://ntfy.sh). |
| `neakasa.py` | Reads the intake ledger (per-meal actual vs planned grams, eat sessions) from Neakasa's app backend. Needs a token captured from the phone app — see the file header. |
| `riko_discover.py`, `riko_tsl_probe.py`, `riko_trace.py`, `riko_poll.py`, `riko_pump_test.py` | Diagnostic scripts used to produce the findings. |

`riko.py --help` and each script's header explain usage. Most read credentials from a
`riko.toml` (see `config.py --example`) or `RIKO_EMAIL` / `RIKO_PASSWORD` env vars.

---

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
