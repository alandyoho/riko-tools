# research/

One-off scripts and notes from working out how the feeder behaves. Nothing here is
run by the monitor or by any service; they are kept because the findings in
`riko-findings.md` and `TESTS.md` came from them.

Run them from the repo root (they import `riko.py` and `config.py` from there), e.g.
`python3 research/riko_trace.py`.

| File | What it was for |
|---|---|
| `riko_trace.py` | Poll the bowl weight rapidly through a feed cycle. |
| `riko_pump_test.py` | Measure how accurately a requested amount of water is delivered. |
| `riko_tsl_probe.py` | Fetch the feeder's data model (its properties, services and events). |
| `riko_cfg_probe.py` | Probe the feeder's config-read service with guessed filenames. |
| `capture_prep.py` | High-frequency polling during a feed cycle. |
| `battery_watch.py` | Low-frequency logger of power state, for the on-battery tests. |
| `crack_feeder_secret.py`, `riko_*.js` | Recovering the app backend's signing secret. |
| `ledger-access-investigation.md` | Notes on reaching the intake ledger. |
