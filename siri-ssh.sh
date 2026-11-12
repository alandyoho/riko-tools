#!/bin/bash
# Forced command for the iPhone Shortcuts key: only "siri.sh <command>" may run.
cmd="$SSH_ORIGINAL_COMMAND"
cmd="${cmd#\~/riko/}"; cmd="${cmd#/home/yoho/riko/}"; cmd="${cmd#./}"
if [[ $cmd =~ ^siri\.sh\ ([a-z]+)$ ]]; then
    exec /home/yoho/riko/siri.sh "${BASH_REMATCH[1]}"
fi
echo "That key can only run feeder voice commands."
exit 1
