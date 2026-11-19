#!/bin/bash
# Run by cron at 08:20, before the 08:30 report: store new Wyze weigh-ins (see cat_weight.py).
cd "$(dirname "$0")" || exit 1
set -a; [[ -f .env ]] && . ./.env; set +a
exec .venv-wyze/bin/python3 cat_weight.py "$@" >> riko_state/cat_weight.log 2>&1
