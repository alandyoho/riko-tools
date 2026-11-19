#!/bin/bash
# Run by cron at 08:30: email yesterday's feeding report (see daily_report.py).
cd "$(dirname "$0")" || exit 1
set -a; [[ -f .env ]] && . ./.env; set +a
exec .venv/bin/python3 daily_report.py "$@" >> riko_state/daily_report.log 2>&1
