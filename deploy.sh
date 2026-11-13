#!/bin/bash
# Update the Pi's ~/riko to what is on GitHub (origin/main), tests first.
# Run on the Pi:  ~/riko/deploy.sh          (or from the Mac: ssh yoho@<pi> riko/deploy.sh)
#
#   1. fetch origin/main
#   2. check it out in a scratch worktree and run tests/run.sh there, with the
#      monitor's own .venv and capture files; the live ~/riko isn't touched yet
#   3. only if every test passes, fast-forward ~/riko to it
#   4. say whether the monitor needs a restart (it does when a .py it runs changed);
#      the restart needs sudo, so it is printed, not run
#
# ~/riko must be a clean checkout: local edits to tracked files stop the deploy
# (copying files over by hand is what this replaces - BUGS.md #17). Untracked
# runtime files (.env, riko_state/, riko_capture/, monitor.db, .venv) are left alone.
set -euo pipefail
cd "$(dirname "$0")"
REPO=$PWD

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "~/riko has local edits to tracked files - not deploying:"; git status --short --untracked-files=no
    exit 1
fi
git fetch -q origin
OLD=$(git rev-parse HEAD) NEW=$(git rev-parse origin/main)
if [[ $OLD == "$NEW" ]]; then
    echo "already up to date ($(git log --oneline -1))"; exit 0
fi
if ! git merge-base --is-ancestor "$OLD" "$NEW"; then
    echo "origin/main is not ahead of ~/riko (diverged) - not deploying"; exit 1
fi
echo "deploying $(git rev-parse --short "$OLD") -> $(git log --oneline -1 "$NEW")"

STAGE=$(mktemp -d /tmp/riko-deploy.XXXXXX)
cleanup() { git -C "$REPO" worktree remove --force "$STAGE" 2>/dev/null || rm -rf "$STAGE"; }
trap cleanup EXIT
git worktree add -q --detach "$STAGE" "$NEW"
ln -s "$REPO/.venv" "$STAGE/.venv"
[[ -d $REPO/riko_capture ]] && ln -s "$REPO/riko_capture" "$STAGE/riko_capture"
if ! (cd "$STAGE" && bash tests/run.sh); then
    echo "tests FAILED on $(git rev-parse --short "$NEW") - ~/riko left at $(git rev-parse --short "$OLD")"
    exit 1
fi

git merge -q --ff-only "$NEW"
echo "~/riko now at $(git log --oneline -1)"
changed=$(git diff --name-only "$OLD" "$NEW")
echo "$changed" | sed 's/^/  changed: /'
if echo "$changed" | grep -E '^[^/]+\.py$' | grep -qvx 'siri.py'; then   # siri.py runs fresh each time
    echo "Python files changed - restart the monitor to load them:"
    echo "  ssh -t yoho@$(hostname -I | awk '{print $1}') 'sudo systemctl restart riko-monitor'"
else
    echo "no Python changes - no restart needed"
fi
