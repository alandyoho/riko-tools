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

### Does the device re-weigh the bowl more than once after serving?
We know it samples on physical bowl removal/insertion and ~10 min after serving.
If that's the only post-meal sample, "retract the tray once the food is gone"
can't be built without physically cycling the bowl.

- Leave a known weight (plastic cap, ~3.8 g) in the bowl and don't touch anything.
- Watch `pushes.jsonl` for an unprompted `bowlStatus` push.
- Partially attempted 2026-09-07; inconclusive, cap was removed before the slot.
- **Why it matters:** blocks the retract-when-empty feature, which is the one that
  stops the cat getting under the machine.

### Food dispensing accuracy
Water is measured and excellent (±0.2 g). Food has never been measured on its own.

- `riko feed 8 0` into a weighed bowl, a few times, at different amounts (4, 8, 16).
- Tip the morsels back into the hopper or count them against the day's intake.
- **Why it matters:** food is the part that affects the cat's calories, and the one
  unexplained result so far (34.25 g delivered on a 32 g target) has to be food.

---

## Recovery and the pump

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

### Why did the noon slot fire at 11:50?
Ten minutes early, with no explanation. The 04:00 and 08:00 slots fired on time.

- Watch the next few slots and see whether it recurs or was a one-off.
- Check whether `bDayEn` or the app's own scheduling is involved.

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

### Pull filenames and service semantics out of the Android APK
`cfgRead` doesn't answer (gateway times out with 20056 on every guessed filename,
while `getVoltage` answers instantly — so it's the service, not the transport).
`udpCmd` produces nothing observable. Both may be factory-tool commands.

- `strings` over the APK's native libs, looking for config filenames.
- Might also reveal what the app sends for "feed now" (which never appears on the
  Aliyun channel — see below).

### Why don't app feeds show on the Aliyun command channel?
The app's tare button shows up; its "feed now" doesn't. Yet the app's history logs
API-issued feeds fine. So the history is device-reported, and the app's feed goes
some other way.

### What does `smartPlanCfg` do?
`{catId: 233099, drierFoodId: 10003}` — implies a cat profile and a food database
on Neakasa's backend. Possibly drives portion recommendations.

### Per-slot portions
`fdPlanStr` stores food and water per slot; the app only exposes one default.
Different portions per meal are possible via `set_schedule` and untested.

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
- **2026-09-07 — Does the bowl retract between meals?** No, not on its own. The tray
  stays out after serving. Only the freshness manager pulls it in early.
