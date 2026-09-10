# Riko — open questions and tests

Running list. Move things to **Answered** with the date and the result rather than
deleting them, so we don't re-run tests we've already done.

---

## High value

### Does the feeder work on battery, with no mains power?
The schedule (`fdPlanStr`) and the clock live on the device, and there's a battery
(90%, `pwrType: 0` = on DC), a `battSavingMode` property, and battery-low/critical
error codes — so it *should* keep feeding through a power cut. Never verified.

- Unplug the Riko 10 minutes before a scheduled slot. Watch the display and see
  whether the meal fires on time.
- Check `pwrStatus.pwrType` flips to 1 and what `batPercentage` does over the cycle.
- Note whether it behaves differently in `battSavingMode`.
- **Why it matters:** this is the difference between a feeder that survives an
  outage and one that silently doesn't. Also decides how much the monitoring layer
  needs to care about power events.

### Does it keep feeding with no internet?
Same reasoning — schedule and clock are local, so it should. But the clock is
already wrong about DST, and we don't know what happens to timekeeping without a
sync source.

- Block the Riko at the router (or pull the router) for 30+ minutes across a slot.
- Confirm the meal fires.
- Afterwards, compare the device's clock to real time — did it drift, and did
  reconnecting re-sync it (and possibly undo our `tz -4` fix)?
- **Why it matters:** decides whether cloud outages are a feeding risk or just a
  monitoring blind spot.

### Is the `sign` header on Neakasa's own API actually enforced?
The app talks to **two** backends with different auth. Aliyun (`us-east-1.api-iot.aliyuncs.com`)
uses standard API Gateway HMAC-SHA1 signing, which our library already does. But the
intake ledger lives on `usapi.neakasapet.com`, which uses `token` + `uid` + `sign`
headers. The `sign` is presumably HMAC over the params with a secret baked into the
app — unextractable on iOS.

Captured request shape:
```
GET https://usapi.neakasapet.com/api/feeder/record
    ?device_name=<serial>&user_id=<id>&bind_status=1
    &start_time=<unix>&end_time=<unix>&data_type=0
headers: token, uid, sign, appid, timestamp, request-id, version
```

- Replay it verbatim with curl. Confirm it returns data.
- Then change `start_time` to a week earlier and resend, **without** changing `sign`.
  If it still returns data, the signature doesn't cover the query params and a
  captured token is enough to read the ledger.
- Also worth learning: how long does the `token` stay valid?
- **Why it matters:** this endpoint has everything the Aliyun channel lacks — per-meal
  planned vs actual grams, failure records with reason codes, and the eat sessions.

### Confirm the over-delivery against the device's own records
The feeder's ledger says it delivered what was planned; two scales say otherwise.

| Source | 16:00 meal |
|---|---|
| Neakasa ledger (`feed_weight` + `water_weight`) | 8 + 23 = 31 g |
| Riko's own load cell | 36 g |
| Kitchen scale | 35.5 g |

So the metering believes it dispensed 8 g of food and the bowl says otherwise. The
overshoot is invisible in the app, which will always show the planned figure.

- Still needs the food-only feed (`riko feed 8 0`) to attribute the excess to food
  rather than water. Deferred — wasteful of food.
- Cheaper alternative: compare `water_weight` in the ledger (consistently 22–23 g
  against a planned 24) with the water-only test results (accurate to ±0.2 g). If
  the device under-reports water it may be under-reporting food too, in which case
  the real dispense is higher than either number.

### Why did the 16:00 meal not produce an eat record?
The captured ledger shows `eat_list` records as timed windows, and only for meals
the bowl was left alone after:

| Meal | Window | Result |
|---|---|---|
| 00:52 (left alone ~2 min) | 19.5 min | ate 34 g, 0 left |
| 04:00 (untouched) | exactly 601 s | ate 28 g, 1 left |
| 08:00 (untouched) | exactly 601 s | ate 28 g, 1 left |
| 12:00-ish manual feeds (bowl lifted) | none | — |
| 16:00 (bowl lifted at +11 s) | none | — |

**Not** the freshness manager — it was off from 00:28, before all of these. The
pattern is that lifting the bowl cancels the eat measurement.

- **Test at the next meal: don't touch the bowl at all.** Confirm an eat record
  appears in the ledger about 10 minutes later.
- The 601-second windows suggest a fixed 10-minute observation period; the 00:52
  one ran 19.5 minutes, so it may end early when the bowl reads empty.
- **Why it matters:** measuring a meal on a kitchen scale and getting the device's
  own intake reading are mutually exclusive on the same feed. Today's overshoot
  numbers cost us three eat records.

### Can intake data be read without the app?
The eat records exist only on `usapi.neakasapet.com` — they never appear as an
Aliyun property push, which is why our watcher has never seen one. So "has the cat
eaten?" is answerable only through that endpoint, or by physically cycling the bowl.

- Depends on the `sign` question above.
- Fallback: freshness manager ON with `leftOverTTH` at its maximum (300 g) — it can
  never decide to retract, but might produce more weight samples. Untested.
- **Why it matters:** blocks the retract-when-empty feature and any intake tracking.

### Food dispensing accuracy — the feeder is over-delivering
Now two consistent measurements of a full 8 g / 24 g meal, both over target:

| When | Bowl | Total | Delivered | Target | Over |
|---|---|---|---|---|---|
| 2026-09-07 ~12:08 | 68.75 g | 103.0 g | 34.25 g | 32 g | +2.25 g |
| 2026-09-07 16:00 | 68.30 g | 103.8 g | 35.50 g | 32 g | +3.50 g |

Water tested clean in isolation (12/24/36 g requested → 11.9/24.1/36.2 g), so the
excess is *probably* food — implying ~10–11.5 g dispensed where 8 was asked for,
28–44% high. At +3 g per meal over six meals that's ~66 g/day against a 48 g target,
which matters for the cat.

**But this is not yet proven.** The water tests were water-only feeds; the pump may
run longer when food is present, to wash the ground food through the chute. That
would put the excess on the water side, where it's harmless.

- `riko feed 8 0` into a weighed bowl — dry food, no water. Repeat at 4, 8, 16 g.
- Tip the morsels back into the hopper, or count them against the day's intake.
- **Why it matters:** decides whether this is a calorie problem or a non-issue.
  Highest priority test on this list.

---

## Recovery and the pump

### [LIVE] First real code-70 under the monitor
The monitor (`monitor.py`) runs as a systemd service `riko-monitor`, currently in
**--dry-run**: it detects and notifies but does NOT remediate. The next actual pump
stall is the long-awaited test of `unclog`'s prime-and-resume, observed safely.

- Watch for a "Pump stalled - not auto-fixing (dry-run)" alert. That confirms
  detection fires on a real stall.
- Then, to actually test recovery: either wait for a later stall after dropping
  --dry-run, or once confident, remove `--dry-run` from
  `/etc/systemd/system/riko-monitor.service` and `sudo systemctl restart riko-monitor`.
- After the first real auto-unclog, read `riko_capture/unclog_*.json` (the journal)
  to confirm prime+resume worked and see whether waterProvide moved water in the
  tray-retracted stall state — the one context we could never reach deliberately.
- **Why it matters:** this is the last unverified assumption in the whole system.

### Does `unclog` actually work?
Both halves are unproven and the next real code 70 settles them.

- **Priming:** `waterProvide` has never been observed to move water. Every attempt
  ran with the tray extended, where the firmware accepts the call and silently
  declines. A real stall has the tray retracted and the device out of freshness
  protection — a state we could not reach deliberately.
- **Resume:** never tested. The app's own retry has failed repeatedly in the past,
  which is why priming stays in.
- `unclog` now journals a full status snapshot at every step to
  `riko_capture/unclog_*.json`. Next stall, run it and read the journal.
- Watch the `scale_g` column across the pulses — that's the only proxy we have for
  water actually moving.

### Do stalls correlate with tank level, or only idle time?
Stalls so far: overnight (long idle), and noon after ~4 h. Successful feeds have
all been within ~30 min of a previous one. But the tank was also getting lower.

- Log tank level alongside every stall.
- Repeat the water-delivery test at a low tank level and compare the rate to the
  full-tank numbers (12/24/36 g → 11.9/24.1/36.2 g, slope 1.012).

### Would a pre-emptive prime eliminate stalls entirely?
The better design if it works: a small water-only feed before each scheduled meal,
issued from a healthy idle state where everything is known to work, with the meal's
water reduced by the primed amount.

- Cost: requires the monitor to issue feeds itself, which makes the Pi
  load-bearing for feeding. Don't do this until the monitor is proven.
- Water accuracy of ±0.2 g means the arithmetic is reliable.

---

## Schedule and timing


### Does the app ever rewrite `timeZoneMsg` back to `zone: -5`?
Our fix has held through one reboot and a full day. Many apps re-sync the device
timezone from the phone on launch.

- Watch for a `timeZoneMsg` push in `pushes.jsonl`.
- If it happens, the monitor needs to re-apply `tz -4` automatically.
- **November 1:** switch to `tz -5` when DST ends, regardless.

### What actually happens on a bowl-missing feed?
Observed 2026-09-07: a feed started with no bowl goes to PREPARING with the tray
*extended* and hangs there indefinitely, flashing on the display. Reseating the
bowl does not release it; only `feedCtrl END` clears it.

- Confirm this is reproducible.
- Worth including in the review — it's a hang, not a clean failure.

---

## Lower priority / rainy day

### Get the app's signing secret (blocked on iOS)
Would answer the `sign` question and possibly the `cfgRead` filenames. iOS apps are
encrypted on device, so this needs an Android phone — `strings` over the native libs
plus `jadx` on the Java. Not worth a jailbreak.

Note the app shares `appKey: 32711645` across both backends, so the Neakasa-side
secret is probably paired to it and baked into the binary.



### What does `smartPlanCfg` do?
`{catId: 233099, drierFoodId: 10003}`. The `catId` matches the cat profile in the
ledger response (name, weight, birthday, breed, spay status), so Neakasa holds a cat
record and probably uses it for portion recommendations. `drierFoodId` presumably
indexes a food database — worth seeing whether changing it alters anything.

### Child lock — what does it actually gate?
`childLockOnOff` is on. Presumed to disable the physical buttons (there's a
retract-bowl button and a grinder button). Untested: toggle it off and press them.

### Sound control
`feedAudCfg` (volume, which sound) and `DND_Mode` (window, days, audio-mute flag)
are the only audio controls. No way to mute button beeps or error tones separately.
Untested: whether DND with audio-mute still lets the feed chime through.

### Nightly Pi reboots
The Pi rebooted at 04:00 and 03:00 on consecutive days, killing the tmux watcher
(now fixed with systemd). Cause unknown — cron, unattended upgrades, a smart plug
schedule, or a power blip. Not a Riko problem but worth finding.

---

## Chores / cleanup

- **[DONE] Removed the mitmproxy CA cert from the iPhone** (2026-09-07). Proxy also off.
- **Rename the ntfy topic to something unguessable.** Currently `riko-sailor` in
  `riko.toml` — short and guessable, so anyone could read the alerts or push fake
  ones. Change it in the toml AND in the ntfy phone app. Also: `riko config` prints
  `_notify` in cleartext; harmless but the topic is a mild secret.
- **Change the Neakasa account password.** It was pasted in plaintext during setup
  and its md5 appears in captured traffic; md5 is unsalted/reversible. Change it and
  don't reuse it. (Then update `.env` / `riko.toml` on the Pi.)
- **Scrub captures before publishing anything.** `riko_capture/tsl_thing_info_get*`
  contains the device secret, MAC, and public IP; intercepted ledger/login bodies
  contain user_id, the md5 password, and signed OSS URLs. All gitignored, but check
  before sharing logs or a repo.


## Answered

- **2026-09-07 — Is the bowl weight live?** No. `curWeight` is cached; polled every
  second through a 197 s dispense and it never moved. Refreshes only on physical
  bowl removal/insertion and ~10 min post-serve.
- **2026-09-07 — Can we force a weight sample in software?** No. `bowlCtrl` moves
  the tray, not the bowl, so no sample. `resetScaleZero` forces a read but re-zeros
  the reference and drops `bBowlIn` — worse than the problem.
- **2026-09-07 — Water delivery accuracy?** Excellent. 12/24/36 g requested →
  11.9/24.1/36.2 g delivered. Slope 1.012, intercept −0.23. No fixed per-feed cost.
- **2026-09-07 — Does the device have a local API?** No. All TCP and UDP ports
  closed, with `udpCmd` both off and on. Sixty seconds of tcpdump showed only ARP.
  Everything goes out over TLS to Aliyun.
- **2026-09-07 — Does the clock fix survive a reboot?** Yes. `zone: -4` held through
  the 04:00 reboot and the 04:00/08:00 feeds fired on time.
- **2026-09-07 — Where does the app's intake data come from?** A second backend,
  `usapi.neakasapet.com`, entirely separate from the Aliyun channel we monitor.
  `/api/feeder/record` returns per-meal planned vs actual grams, failure records
  with reason codes, and eat sessions. Nothing of this crosses Aliyun, which is why
  the watcher never saw a post-serve sample — there was never one to see.
- **2026-09-07 — Does a stalled feed grind food before failing?** Yes. The noon
  stall's ledger record shows `feed_weight: 5, water_weight: 0, status: 2` — 5 g
  ground, no water, failed. Confirms the grinder runs before the pump, and validates
  why `feedCtrl END` + re-feed double-doses.
- **2026-09-07 — Does the bowl retract between meals?** No, not on its own. The tray
  stays out after serving. Only the freshness manager pulls it in early.
- **2026-09-07 — Does the device re-weigh periodically?** No. A plastic cap sat in
  the bowl for 90 minutes untouched; the reported weight never changed and never
  picked up the cap. Samples happen only on physical bowl removal/insertion and
  (usually) ~10 min after serving.
- **2026-09-07 — Why do scheduled meals start early?** By design. The feeder works
  backwards from its own time estimate (soak + ~40 s per gram of food) so the meal
  is ready *at* the slot time, then holds the soaked food until the slot before
  serving. The 16:00 meal: PREPARING 15:50:02, SERVING 16:00:01, IDLE 16:00:06 —
  exact to the second. The noon slot's 11:50 start was this, not a bug.
- **2026-09-07 — Do scheduled feeds apply the soak?** Yes; manual feeds don't. The
  16:00 meal ground early then showed a 6:50 countdown before serving. Note the
  soak setting is effectively a *minimum* — the real hold is whatever time is left
  between finishing the grind and the scheduled slot.
