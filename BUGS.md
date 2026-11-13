# Riko — known bugs

What we know is broken, most serious first within each group. Evidence and test details
live in TESTS.md and riko-findings.md. When something is fixed or worked around, say how
and when rather than deleting it.

---

## Feeder firmware (1.0.1-0007) — we can only work around these

1. **It skips the first scheduled meal after switching to battery.** 6 of 6 since
   September (09-09, 09-10, 09-16, 09-26, 11-09 15:55 and 19:55). It wakes ~7 minutes
   before the slot, reports IDLE for 23-59 s, and sleeps again. No ledger record at all,
   not even a failure. The second meal ran both times we tried (11-09 17:55, 21:55), but
   only after it had been woken and fed by hand in between. Any power cut costs at least
   one meal, silently. Likely cause (11-11): it wakes at the right real time, but its
   clock is wrong after sleep (80 min fast, 15 min slow), so the meal isn't "now"; synced,
   it tried the meal by itself within 36 s.
2. **Its clock is unreliable across power changes.** Jumped 80 min ahead once on battery
   (11-09); gains ~1 min/hour asleep on battery; lost 20-30 min after short unplugs in
   Sept/Oct and never corrected itself. A wrong clock shifts every meal; a jump can skip one.
3. **Grinder jams (code 13) never clear by themselves.** After 10 minutes it gives up and
   serves a partial meal. Pause + resume does not clear one (0 of 10, 11-05), and each
   resume restarts the 10-minute timer. Mostly the duck food (~2.7 a day; chicken none).
4. **A jam-delayed meal can come out short on water.** When the soak timer runs out it
   serves wherever it is in the meal (5 g + 21 g instead of 5 + 35 on 11-05).
5. **The ledger under-reports what is served.** Meals land 2-5 g over the record, mostly
   water dripping after each dose. Ledger times are the feeder's own clock.
6. **On battery it cannot be reached between meals.** Remote feeds and schedule edits fail
   while it sleeps (error 20056); the app says remote manual feeding is unavailable.
7. **A feed with no bowl hangs in "preparing"** until feedCtrl END. Seen on the old
   firmware, not rechecked on 0007.
8. **Lifting the bowl cancels that meal's eat record.**
9. **The app resets an edited meal to a 1:5 food:water ratio.** Write schedules through
   the API (riko.Schedule) instead.
10. **A security issue in Neakasa's cloud API.** Details are kept out of this public repo.

## Monitor / Pi

11. **The clock keeper can hold off a needed sync,** thinking it would repeat a meal that in
    fact already passed with no record (11-09 15:55). In an outage that leaves the clock
    wrong for up to an hour.
    *Fixed 2026-11-11: a fast clock is synced anyway when the meal has no record.*
12. **No alert when the feeder goes on battery.** Only the Pi's own UPS is watched, so a
    knocked-out feeder plug goes unnoticed until a meal is missed.
    *Fixed 2026-11-11: "Feeder is on battery" / "back on wall power" alerts.*
13. **No rescue for a skipped battery meal.** The missed-meal check only alerts, and the
    30 s pass is too slow to catch the 23-59 s wake.
    *Built 2026-11-11, not yet proven live: fast cloud-status poll near meals on battery,
    feed sent when it wakes idle; a button press before the next meal's prep is the second
    chance, then the meal is dropped (never doubled up); clock checked at the switch and
    every wake. Next: a hands-off unplug test, then a SwitchBot press as the fallback.*
    *Proven live 2026-11-11 13:55: woke 13:47:23, clock synced, meal sent 13:47:31, served
    13:52:46, no help. Side effect, still open: the feeder's own attempt at that meal
    (13:48:00) was rejected "device busy" and raised a false urgent "Feed failed" alert.*
    *SwitchBot fallback built 2026-11-11 (off until tested on battery): presses the feed
    button at slot + 1 min when the meal is still owed and the feeder is asleep.*
    *2026-11-12: the SwitchBot wake proven live on battery (awake 20 s after the command,
    ~37 s up); turned on in the monitor (RIKO_SWITCHBOT=on). The false "device busy"
    alert fixed: a refusal "busy (FoodPreparing/FoodDelivering)" with another feed served
    within 15 min, or the feeder preparing/serving right now, is a "duplicate turned away"
    note; with no other meal in sight it is still a "Feed failed" alert (tests/test_busy.py).*
14. **A manual feed can hide a missed meal.** The missed-meal check counts any feed within
    ~11 minutes of the slot as that meal's record (how the 11-09 15:55 miss went unalerted).
    The reverse is fixed (2026-11-11): a meal the monitor saw preparing or serving isn't
    reported missed while its ledger record is late (battery meals may upload late).
    *Fixed 2026-11-12: only the meal's own (auto) record counts; a manual feed near the
    slot turns the alert into a note ("a manual feed covered it"), and a water-only one
    doesn't count at all.*
15. **The water top-up can only add water after serving,** and not once he is eating. By
    design, but a jam-delayed meal can still end up under-soaked.
16. **The monitor doesn't know firmware 0007's error code 63** ("cat interference –
    weighing"); it would report it as unknown.
    *Fixed 2026-11-12: codes 15, 63 and 72 (cat interference) are now named.*
17. **Deploy drift on the Pi.** ~/riko is an old git checkout updated by copying files, so
    stale files linger (an outdated tests/test_topup.py failed on 11-05).
    *Fixed 2026-11-12: ~/riko is now a clean checkout of origin/main, updated only by
    deploy.sh (fetch, run the tests in a scratch worktree, fast-forward if they pass,
    say whether to restart). ~/riko-stage is retired.*
18. **The Pi's UPS lasts ~2 hours** and its low-battery safe-idle path has never run on the
    real Pi. The feeder's battery outlasts the Pi.
19. **The Pi keeps only about a day of system logs** (on 11-11 the oldest monitor entry was
    11-10 21:47), so incidents have to be read within a day; riko_capture/pushes.jsonl and
    monitor.db are the long-term record.
    *Fixed 2026-11-12: the journal is on disk now (Pi OS keeps it in RAM by default),
    capped at 250 MB with 1 GB kept free: /etc/systemd/journald.conf.d/50-riko-persistent.conf.
    About a month at ~8 MB/day; it survives reboots.*
20. **Cellular "relay" mode (router up, internet down) failed its live test** and is off,
    so that case isn't covered.

## Housekeeping

21. **Rename the ntfy topic to something unguessable.** Anyone who knows a topic can read
    its alerts or send fake ones.
22. **Change the Neakasa account password.** It was exposed during setup.
