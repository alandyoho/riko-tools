#!/bin/bash
# Entry point for iPhone Shortcuts ("Run Script over SSH"): siri.sh <command>
# Prints one sentence for Siri to speak. See siri.py for the commands.
cd "$(dirname "$0")" || exit 1
set -a; [[ -f .env ]] && . ./.env; set +a
exec .venv/bin/python3 siri.py "$@" 2>/dev/null
