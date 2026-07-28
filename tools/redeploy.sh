#!/usr/bin/env sh
# Rebuild and restart the stack, then prove it actually came up.
#
# Written because "I built the image" and "the running app changed" are not the
# same thing, and the gap between them is invisible: `docker build` succeeds, the
# old container keeps serving, and you test yesterday's code while reading
# today's diff.
#
#   tools/redeploy.sh              rebuild changed services, restart, verify
#   tools/redeploy.sh --wrapper    also restart the HOST wrapper (see below)
#   tools/redeploy.sh --quiet      for the post-commit hook
#
# The host-wrapper is deliberately NOT restarted by default. It runs on the host
# rather than in Docker so it can shell out to the `claude` CLI with your logged-in
# credentials, it is usually running in a terminal you own, and killing it out
# from under you to pick up an unrelated engine change is rude. Pass --wrapper
# when you actually changed host-wrapper/, and the script will tell you when you
# did and forgot.

set -e

cd "$(dirname "$0")/.."
ROOT=$(pwd)
LOG="$ROOT/.deploy.log"
QUIET=0
WRAPPER=0

for arg in "$@"; do
  case "$arg" in
    --quiet)   QUIET=1 ;;
    --wrapper) WRAPPER=1 ;;
  esac
done

say() { [ "$QUIET" = "1" ] || printf '%s\n' "$*"; printf '%s\n' "$*" >> "$LOG"; }

say "── redeploy $(date '+%Y-%m-%d %H:%M:%S') ──"

# ── 1. the engine's tests are the gate ──────────────────────────────────────
# Deploying a red build to your own machine wastes more time than the 3 seconds
# this costs, because the symptom shows up as a confusing runtime error.
if command -v python >/dev/null 2>&1; then
  if (cd services/engine && python -m pytest tests/ -q >/dev/null 2>&1); then
    say "tests: pass"
  else
    say "tests: FAIL — refusing to deploy. Run: cd services/engine && python -m pytest tests/ -q"
    exit 1
  fi
fi

# ── 2. rebuild and restart the containers ───────────────────────────────────
# --build on `up` rather than a separate `build`: it rebuilds AND swaps the
# running container in one step, which is exactly the gap described above.
say "building + restarting containers…"
if ! docker compose up -d --build >> "$LOG" 2>&1; then
  say "docker compose failed — see $LOG"
  exit 1
fi

# ── 3. verify, do not assume ────────────────────────────────────────────────
# A container that is 'Up' can still be serving a stack trace.
say "waiting for the engine…"
ok=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if curl -fs -m 3 http://127.0.0.1:8091/api/health >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done

if [ "$ok" != "1" ]; then
  say "engine did not become healthy — last container logs:"
  docker compose logs --tail 30 engine 2>&1 | tee -a "$LOG"
  exit 1
fi

# Check a route that only exists in the NEW code. Health alone would pass on a
# stale container, which is the precise failure this script exists to catch.
if curl -fs -m 3 http://127.0.0.1:8091/api/scenarios >/dev/null 2>&1; then
  # Count "blurb", not "id": every nested playable character carries an id too,
  # so counting ids reports 11 scenarios for a world with 1 and 10 characters.
  count=$(curl -fs -m 3 http://127.0.0.1:8091/api/scenarios | grep -o '"blurb"' | wc -l | tr -d ' ')
  say "engine: healthy, serving $count scenario(s)"
else
  say "engine: healthy but /api/scenarios is missing — you are on stale code"
  exit 1
fi

# ── 4. the host wrapper ─────────────────────────────────────────────────────
if [ "$WRAPPER" = "1" ]; then
  say "restarting host-wrapper…"
  # Only the wrapper's own port, so we never kill an unrelated python.
  pid=$(netstat -ano 2>/dev/null | grep ':5501 ' | grep LISTENING | awk '{print $5}' | head -1)
  [ -n "$pid" ] && taskkill //PID "$pid" //F >/dev/null 2>&1 || true
  (cd host-wrapper && cmd //c start.bat >> "$LOG" 2>&1 &)
  sleep 3
fi

if curl -fs -m 3 http://127.0.0.1:5501/providers >/dev/null 2>&1; then
  say "wrapper: up"
else
  say "wrapper: DOWN — start it with host-wrapper/start.bat (turns will degrade"
  say "         to raw event lists until it is back)"
fi

# Nudge only when it matters: wrapper source changed but it was not restarted.
if [ "$WRAPPER" != "1" ] && git diff --name-only HEAD~1 HEAD 2>/dev/null | grep -q '^host-wrapper/'; then
  say "NOTE: host-wrapper/ changed in this commit but was not restarted."
  say "      Re-run with: tools/redeploy.sh --wrapper"
fi

say "done → http://127.0.0.1:8091"
