#!/bin/bash
# Run every test. They need the Python environment the monitor uses (the Neakasa
# SDK), so run this on the machine the monitor runs on:
#
#   tests/run.sh
#
# Nothing here talks to the feeder or changes anything: the feeder, the ledger and
# the clock are all simulated. Two tests replay real data when it is available:
#   * over-dispense: a ledger dump, fetched fresh if you pass --ledger
#   * power: a captured properties file from riko_capture/, used if present
set -uo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python3; [[ -x $PY ]] || PY=python3
LEDGER=""
if [[ ${1:-} == --ledger ]]; then
    LEDGER=$(mktemp)
    set -a; [[ -f .env ]] && . ./.env; set +a
    $PY neakasa.py raw --days 18 > "$LEDGER" 2>/dev/null || true
    [[ -s $LEDGER ]] || { echo "couldn't fetch the ledger; skipping the replay"; LEDGER=""; }
fi
PROPS=$(ls riko_capture/props_*.json 2>/dev/null | head -1)
fail=0
run() { echo "== $1"; shift; "$@" 2>&1 | grep -vE '^[0-9]{4}-[0-9]{2}-[0-9]{2} |^riko' ; [[ ${PIPESTATUS[0]} -eq 0 ]] || { echo "FAILED"; fail=1; }; }
run "top-up"        $PY tests/test_topup.py
run "over-dispense" $PY tests/test_overdispense.py $LEDGER
run "clock"         $PY tests/test_clock.py
run "grinder jam"   $PY tests/test_jam.py
run "power"         $PY tests/test_power.py $PROPS
run "failover"      $PY tests/test_failover.py
run "session"       $PY tests/test_session.py
run "water ratio"   bash tests/test_ratio.sh
[[ -n $LEDGER ]] && rm -f "$LEDGER"
[[ $fail -eq 0 ]] && echo "all tests passed" || { echo "some tests FAILED"; exit 1; }
