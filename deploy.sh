#!/bin/bash
# Update the Pi's ~/riko to what is on GitHub (origin/main), tests first.
# Run on the Pi:  ~/riko/deploy.sh          (or from the Mac: ssh yoho@<pi> riko/deploy.sh)
#
#   1. fetch origin/main
#   2. check it out in a scratch worktree and run tests/run.sh there, with the
#      monitor's own .venv and capture files; the live ~/riko isn't touched yet
#   3. only if every test passes, fast-forward ~/riko to it
#   4. say which services need a restart (those whose script or its imports changed);
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
# Which running services load a changed file: each riko-*.service's script plus every
# local module it imports, followed through. Scripts run fresh each time (siri.py,
# daily_report.py, cat_weight.py from cron or the phone) never need a restart.
services=$(for f in /etc/systemd/system/riko-*.service; do
    [[ -e $f ]] || continue
    script=$(grep -o 'python3 [A-Za-z0-9_]*\.py' "$f" | head -1 | cut -d' ' -f2)
    [[ -n $script ]] && echo "$(basename "$f" .service) $script"
done)
restart=$(echo "$changed" | .venv/bin/python3 -c '
import ast, sys
from pathlib import Path
changed = {l.strip() for l in sys.stdin if l.strip().endswith(".py") and "/" not in l.strip()}
def local_imports(path, seen):
    if path in seen or not Path(path).exists():
        return seen
    seen.add(path)
    for node in ast.walk(ast.parse(Path(path).read_text())):
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else []
        for n in names:
            local_imports(n.split(".")[0] + ".py", seen)
    return seen
for line in sys.argv[1].splitlines():
    unit, script = line.split()
    if changed & local_imports(script, set()):
        print(unit)
' "$services")
if [[ -n $restart ]]; then
    echo "these services run changed code - restart them to load it:"
    echo "  ssh -t yoho@$(hostname -I | awk '{print $1}') 'sudo systemctl restart $(echo $restart)'"
else
    echo "no running service uses the changed files - no restart needed"
fi
