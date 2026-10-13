# Neakasa Riko — findings

Everything below was determined by talking to the feeder through its cloud API and
checking the results against a kitchen scale. Firmware 1.0.0-0020.

Most of this was written on 2026-09-07. Firmware has since moved to 1.0.0-0033 and
several findings changed. Those are marked **Update** with a date where they sit, and
the newer findings are in "Found later" below. TESTS.md has the evidence for each.

---

## Current state (2026-09-07)

Everything below was learned over two days of investigation. Where it stands now:

- **Control: solved.** `riko.py` logs in with the account credentials and fully
  controls the feeder through the Aliyun channel — status, feed, schedule, tare,
  timezone, error decode, stall recovery. No manual steps.
- **Recording: running.** `riko-watch` (systemd) captures every device event to
  `riko_capture/pushes.jsonl`, survives reboots.
- **Monitoring: running in dry-run.** `riko-monitor` (systemd) detects problems and
  will fix the safe ones (pump stall -> unclog, clock drift -> re-apply tz) under
  hard caps, with ntfy push alerts. Currently `--dry-run` (detects + notifies, does
  not act) until the first real pump stall has been observed being handled.
- **Fixes applied and holding:** clock set to UTC-4 (firmware ignores DST), bowl
  weight corrected to 68 g (factory 65 was wrong), freshness manager off.
- **Intake ledger: read-only via captured token.** The cat's actual eating and true
  dispensed grams live on Neakasa's app backend behind an AES key baked into the
  Flutter binary. Readable with a hand-captured token (`neakasa.py`), not yet
  self-sufficient — see key-extraction-plan.md.

Open items live in TESTS.md; cleanup chores at the bottom of it.

**Update 2026-10-12 — what has changed since this snapshot:**

- The monitor is live, not dry-run, and does more: it tops up or skips a meal when the
  last one wasn't eaten, alerts on food the feeder didn't count, keeps the feeder's
  clock right, and handles grinder jams and the Pi's battery backup. See the README.
- The intake ledger is self-sufficient: `neakasa.py` reads it with a normal login.
- The daylight-saving bug was fixed by Neakasa in firmware 1.0.0-0023.
- The feeder reports a live scale reading and its own clock on request (`iotRead`).


## Bugs, with workarounds

### The clock ignores daylight saving time
The feeder holds correct timezone data — America/New_York, UTC-5, DST flag set,
correct DST start and end dates — and then doesn't apply the DST flag. It runs an
hour behind for the whole DST period.

Every consequence follows from this:
- Scheduled meals fire an hour late.
- The app uses the correct time, sees the slot pass with nothing happening, and
  marks the meal **"Expired."**
- The "random make-up feeding" people report is the real meal arriving an hour
  later, after they've already fed by hand.

There is no time or timezone setting in the app. Writing `timeZoneMsg` with
`zone: -4, dst: 0` corrects it immediately and survives a reboot. Revert to
`zone: -5` when DST ends (Nov 1).

**Update:** fixed in firmware 1.0.0-0023. The honest setting (`zone: -5, dst: 1`) is
now the right one, and the feeder applies the hour itself. A different clock fault
remains — see "The clock falls behind after a power interruption" below.

### The stored empty-bowl weight is wrong
Food-in-bowl is computed as scale reading minus a stored empty-bowl weight. The
stored value is **65 g**. The bowls that ship with the unit weigh **67.9–68.7 g**
(measured: 67.9, 68.35, 68.4, 68.75 across four). So every reading carries about
3 g of phantom food — roughly 40% of an 8 g meal.

The scale itself is accurate: a served meal read 103 g on the feeder and 103 g on
a kitchen scale. The bias is a consistent ~0.3–1 g low, within whole-gram rounding.

The app's tare button doesn't fix it — it zeroes the platform and leaves the 65
alone. The underlying command (`resetScaleZero`) accepts a bowl-weight argument;
the app just never sends one. Sending 68 fixes it permanently: an empty bowl now
reads 0.

**Update:** on earlier firmware the app reset the corrected value to 65 after updates.
It has held at 68 since 1.0.0-0033.

### "Nozzle clogged" is a pump that lost its prime
The device's own error is `pumper: Stuck` (code 70). Water drains back toward the
tank while the feeder is idle, and small pumps push water well but can't pull air.
The firmware's flow check gives up after about a second and calls it a clog.

- Stalls have only happened after long idle gaps (overnight, and once after four
  hours). Feeds within ~30 minutes of a previous one have never stalled.
- The fix needs no disassembly. What has worked both times is getting a fresh feed
  cycle to run.
- The app's retry button *resumes* the stalled attempt, which doesn't re-prime, so
  it fails the same way.
- Unscrewing and reseating the nozzles works because taking them apart moves water
  back into the pump — not because anything was blocked.

### The "freshness" feature can block feeds
On by default: retracts the bowl if it thinks food is left over (20 g threshold,
4-hour window). Combined with the phantom 3 g and stale weight readings, the feeder
can decide there are leftovers, pull the bowl in, and then refuse to start the next
meal — reporting a "retract-bowl button" error instead. Extending the bowl clears
it. Turning the feature off avoids the whole class of problem.

This is the likely explanation for the "chimes but doesn't dispense until I reset
the scale" reports.

### A feed started with no bowl hangs
It doesn't fail cleanly. The feeder goes to "preparing" with the tray extended and
sits there indefinitely, flashing on the display. Reseating the bowl does not
release it; the feed has to be cancelled.

### The clock falls behind after a power interruption
*(Found 2026-10-12, firmware 1.0.0-0033.)* Unplugging the feeder for a few minutes
leaves its clock 20–30 minutes slow, and it never corrects itself:

- 2026-09-27: on battery 20:32–20:37 (5 min). The next four meals each started
  28.2 minutes late.
- 2026-10-11: on battery 20:17–20:20 (3 min). The 23:55 meal started preparing at
  00:07:43, and the feeder's midnight rollover arrived at 00:22:43 — 22.7 minutes slow.

The lost time is not the length of the outage. Every meal after that runs late by the
same amount until the clock is synced (`updateTime`), with nothing shown in the app.

Syncing is not free of risk either: if the clock jumps from before a meal's start to
more than about five minutes past its slot, the feeder marks that meal "Expired" and
does not serve it. A jump that landed 5 minutes after a slot did serve (2026-09-28);
one that landed 6 minutes after did not (2026-10-12).

### Food the feeder doesn't count
*(Found 2026-10-11 and 2026-10-12, firmware 1.0.0-0033.)* The feeder grinds until its
scale sees the target weight (see "The feeder doses by weight" below), and its record
of a successful meal always equals the plan. Food that hangs up between the grinder
and the bowl is ground without being counted, and lands later, dry:

- **Dry food on top of a soaked meal.** A meal recorded as a normal 8 g + 56 g left
  76 g in the bowl, with dry food lying on top; 12 g had to be removed. Three meals
  out of about a hundred came out 9–12 g over what the record explains.
- **A dump after a jam.** With the grinder reporting "jammed with food" (code 13), the
  food that had built up came out at once: the meal was recorded as 0 g food + 27 g
  water and left 83 g in the bowl, about 56 g uncounted. A resume from the app did not
  clear the jam.

Take the bowl off before resuming or clearing a jam.

### The device leaks its own credentials
The device-info endpoint the app uses returns the feeder's cloud authentication
secret — to a *shared* account, not just the owner. That's a security problem
rather than a bug.

---

## How the machine actually behaves

### The bowl weight is not live
**Update 2026-10-12:** this section describes the `bowlStatus` property, and it is still
true of that property — on 1.0.0-0033 it only updates when the bowl is taken off or
put back. But the conclusions below no longer hold, because there are two other
sources: the feeder returns a **live scale reading on request** (`iotRead` type 2, to
0.01 g, and it works mid-meal), and the intake ledger logs eating in 10-minute windows
with "ate X, left Y". So software can tell when the bowl is empty.

The reported weight is a cached value. Polling it every second through a
197-second dispense showed no change at all. It refreshes only when:
- the bowl physically leaves or returns to the tray, and
- roughly 10 minutes after serving (this is what produces the app's "Ate Xg,
  Yg left" line).

Anything the cat eats after that 10-minute sample never registers, which is why
the app can keep showing food in a bowl that's been licked clean.

Confirmed by leaving a known weight (a small plastic cap) in the bowl and not
touching anything for 90 minutes: the reported weight never changed, and never
picked up the cap. There is no periodic sample.

There is no software way to force one either. Retracting the tray doesn't do it —
the bowl never leaves the load cell, so no sample is taken. Taring forces a read
but also re-zeros the reference and reports the bowl as absent, which is worse
than the problem.

The practical consequence: nothing outside the feeder can tell when the cat has
finished eating, except by reading that single 10-minute sample. Any automation
that wants to react to an empty bowl — retracting the tray, say — either has to
work from that one reading or fall back to a timer.

### Timing: soak time plus ~40 seconds per gram of food
Mapped by reading the app's estimate across portion sizes:

| Food | Water | Estimate |
|---|---|---|
| 3 g | 9 g | 7 min |
| 5 g | 15 g | 9 min |
| 8 g | 24 g | 11 min |
| 9 g | 18 g | 11 min |
| 12 g | 36 g | 13 min |
| 15 g | 45 g | 15 min |
| any | 0 g | 1 min |
| 0 g | any | 1 min |

Water quantity makes no difference — 9 g of food with 18 g of water and 8 g with
24 g both estimate 11 minutes. Changing the soak from 5 to 10 minutes adds 5 to
every estimate. So: **estimate = soak + ~0.67 min per gram of food.** The grinder
mills to order, which is the per-gram cost.

This explains scheduled meals starting early — a noon slot began at 11:50. The
feeder works backwards from this estimate so the food is ready *at* the scheduled
time. If you lengthen the soak, every meal starts proportionally earlier.

Actual cycles run well under the estimate: 110–197 seconds for an 8 g/24 g meal.
The estimate is a conservative budget, not a measurement.

### Manual feeds skip the soak
**Update 2026-10-12:** no longer true. On firmware 1.0.0-0033 a manual feed soaks like a
scheduled one: a traced 4 g + 28 g manual meal finished dispensing at 140 s and was
served at 743 s, exactly 10 minutes later. The text below describes 1.0.0-0020.

"Feed now" grinds, adds water, and serves immediately — about two minutes — from
both the app and the API. The rehydration time only applies to scheduled meals.
The app still quotes the full soak in its estimate, so it says 11 minutes for
something that takes two.

### Water delivery is excellent
Requested 12, 24, and 36 g in separate water-only feeds; delivered 11.9, 24.1, and
36.2 g. That's a slope of 1.012 with a −0.23 g intercept — accurate to about a
fifth of a gram, with no fixed per-feed cost. Food accuracy hasn't been measured
separately yet, but a full 8 g/24 g meal came in at 34.25 g against a 32 g target,
so the ~2 g of slack is on the food side.

**Update 2026-10-12:** a live scale trace supports that. A 4 g + 28 g meal ended at
34.0 g; the four water doses totalled 28.2 g, and the extra arrived as a slow creep
after the main food drop and between water doses (about 3 g in all). The first stretch
of creep came before any water, so that part is food. A food-only trace would confirm
the rest.

### The feeder doses by weight
*(Found 2026-10-12.)* There is one scale, under the bowl, and the bowl sits on it under
the outlet for the whole preparation. A live trace of a meal shows the food landing
first, then the water in equal measured doses a few seconds long, about 25 seconds
apart (7.02, 7.04, 7.08 and 7.02 g for a 28 g order). Its error list includes "cat
interference" during food dispensing (code 15) and water dispensing (code 72), and a
meal cut short gets water in proportion to the food that actually arrived.

The other sensors it exposes: an infrared sensor on the food bin, a water-level
sensor, the grinder motor's voltage, the pump, a temperature sensor.

### The bowl does not retract between meals
After serving, the tray stays out until the next meal. The only thing that pulls it
in early is the freshness feature. There are explicit retract and extend commands,
but issuing a retract outside a feed cycle drops the device into "freshness
protection" — even with the feature switched off — where the pump won't run.

### There is no local control
All TCP and UDP ports are closed. Sixty seconds of packet capture showed nothing
but ARP. Everything goes out over TLS to Alibaba Cloud's IoT platform, which the
feeder shares with the M1 litter box. The schedule and clock do live on the device,
so it should keep feeding through a network outage — but the app, notifications,
and any external monitoring all depend on the cloud being up.

### Several diagnostic commands appear to be inert
The device declares a factory-style toolkit — read/write config files, toggle a UDP
channel, run the pump directly, read raw sensor voltages, fake an error report. Of
those, only the voltage read demonstrably works. Config reads time out, the UDP
toggle opens nothing, and direct pump control has never moved water in any state
we could reach.

**Update 2026-10-12:** one more works, and it is the useful one. `iotRead` returns, on
request: the feeder's local time (type 0), a live scale reading (type 2), the food-bin
sensor value (type 4) and the power state (type 3).

---

## The two backends, and what's reachable

The app talks to two separate clouds, with two different auth schemes:

**Aliyun IoT (`*.api-iot.aliyuncs.com`, MQTT on `*.itls.*`)** — the device channel.
Standard Alibaba API Gateway signing (`x-ca-signature`, HMAC-SHA1 over named
headers), which the open-source M1 SDK already implements. This is fully ours: log
in with the account email + password, then read every device property, invoke every
service, and stream status. Everything operational lives here and needs nothing but
the user's own credentials.

**Neakasa's own backend (`usapi.neakasapet.com`)** — the app-data channel. This is
where the intake ledger, cat profile, food database, and app config live. None of it
crosses the Aliyun channel, which is why the property watcher never saw a post-serve
weight sample — there was never one to see. Every request here carries three
app-computed headers:
- `token` — AES-encrypted (no padding, 64-byte ciphertext), rotates per session
- `uid` — AES-encrypted user id (16-byte ciphertext of the numeric id)
- `sign` — a signature; enforced on login, but NOT verified against query params on
  `/feeder/record` (a captured sign kept working after the params changed)

Because `sign` isn't checked on the ledger, a captured `token`/`uid`/`sign` triple
reads the ledger for as long as the token stays valid — but we can't mint or refresh
one ourselves, because the AES key and the login signing key are baked into the app.

### Why the key couldn't be extracted (yet)

The app is **Flutter**. All business logic is compiled Dart in `libapp.so` (~9.7 MB
of native ARM); the decompilable Java is only SDK glue (Aliyun, JPush, Zendesk,
retrofit). The crypto is Neakasa's own — the binary names
`package:flutter_module/common/utils/aes/AESNoPadding.dart`, `CryptoPKCS7.dart`,
`MD5.dart`, and `RequestInterceptor.dart` (where headers are attached).

Static extraction was exhausted: every key-shaped constant in `libapp.so` was tested
against a known plaintext/ciphertext oracle — the `uid` header decrypts to the known
numeric user id `400133257` — across AES-ECB and CBC, 16/24/32-byte keys, hex and
ASCII interpretations, zero and key-derived IVs. None matched. So the key is
constructed at runtime, stored as raw non-printable bytes, or derived — not a plain
stored string. Getting it requires running the app and observing the AES call, which
is a Flutter runtime teardown (see the separate plan).

### The endpoints behind that auth (observed)

- `POST /api/login/user` — email + md5(password); returns login_token and the Aliyun
  bridge token. Signing it ourselves would make the whole API self-sufficient.
- `GET /api/feeder/record` — the intake ledger (per-meal planned vs actual, failure
  reasons, eat sessions).
- `GET /api/feeder/record/statistics` — aggregates.
- `GET /api/getDeviceStsByUser`, `/api/getUserSts` — device/user status.
- `GET /api/app/config`, `/api/synchronizeAppInfo` — config, banners, feature flags.
- Password is sent as **unsalted md5** — reversible for common passwords; treat that
  hash as the password.

### What the ledger reveals that the device channel doesn't

The device's own metering under-reports: the 16:00 meal logged `feed_weight: 8,
water_weight: 23` (31 g) while two scales read 35.5-36 g. So the overshoot is real
and invisible in the app — the ledger always shows the planned figure. And the eat
records only exist when the bowl is left alone after serving; lifting it to weigh
cancels the measurement.

## Summary

The hardware is good. Portions are accurate, the scale is accurate, the pump works,
the bowl mechanism works. Every problem here is software:

- apply the daylight saving flag
- ship the right bowl weight, or send it from the tare button
- give the pump a second chance before declaring it clogged
- re-weigh the bowl more than once after a meal
- fail cleanly when the bowl is missing instead of hanging
- don't hand out device credentials to shared accounts

Added 2026-10-12, from the later findings:

- re-sync the clock when power returns or the feeder reconnects, and periodically
- count food by what reaches the bowl after the grinder stops, and stop grinding when
  nothing is arriving instead of building up a plug behind a jam
- let the app set more than 1:5 food to water, and more than 10 minutes of soak
- allow more than one login per account

**On the tooling side:** device control is fully self-sufficient through the Aliyun
channel. The app's own backend (intake ledger, cat profile) is readable only with a
manually captured token, because the auth is gated by an AES key compiled into the
Flutter binary that static analysis could not recover. Making that self-sufficient
is a separate effort — see the key-extraction plan.

**Update:** that effort succeeded. `neakasa.py` now logs in with the account's own
credentials and reads the ledger with no captured token.
