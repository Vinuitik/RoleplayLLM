#!/usr/bin/env sh
# Rebuild and restart EVERYTHING, then prove it is running this commit.
#
# Written because "I built the image" and "the running app changed" are not the
# same thing, and the gap between them is invisible: `docker build` succeeds, the
# old container keeps serving, and you test yesterday's code while reading
# today's diff.
#
# The host-wrapper is the worst offender. It runs on the host rather than in
# Docker — it has to, so it can shell out to the `claude` CLI with your logged-in
# credentials — which means it outlives every `docker compose up` and quietly
# keeps serving whatever it was launched with. You rebuild the stack, everything
# reports healthy, and the router is three commits stale.
#
# So this script does not ask whether things restarted. It asks both services
# what commit they are running and refuses to pass unless they match HEAD.
#
#   tools/redeploy.sh                 rebuild, restart all, verify
#   tools/redeploy.sh --no-wrapper    leave the host wrapper alone
#   tools/redeploy.sh --quiet         for the post-commit hook

set -e

cd "$(dirname "$0")/.."
ROOT=$(pwd)
LOG="$ROOT/.deploy.log"
QUIET=0
WRAPPER=1          # ON by default: a stale wrapper is the whole problem.

for arg in "$@"; do
  case "$arg" in
    --quiet)       QUIET=1 ;;
    --no-wrapper)  WRAPPER=0 ;;
  esac
done

say() { [ "$QUIET" = "1" ] || printf '%s\n' "$*"; printf '%s\n' "$*" >> "$LOG"; }
fail() { say "FAILED: $*"; exit 1; }

SHA=$(git rev-parse HEAD 2>/dev/null || echo unknown)
SHORT=$(printf '%s' "$SHA" | cut -c1-8)

say "── redeploy $(date '+%Y-%m-%d %H:%M:%S')  →  $SHORT ──"

# ── 1. tests gate the deploy ────────────────────────────────────────────────
# Three seconds here beats debugging a red build through the UI.
if command -v python >/dev/null 2>&1; then
  if (cd services/engine && python -m pytest tests/ -q >/dev/null 2>&1); then
    say "tests: pass"
  else
    fail "tests are red. Run: cd services/engine && python -m pytest tests/ -q"
  fi
fi

# ── 2. containers ───────────────────────────────────────────────────────────
# --build on `up` rather than a separate `build`: it rebuilds AND swaps the
# running container in one step, which is exactly the gap described above.
say "building + restarting containers…"
GIT_SHA="$SHA" docker compose up -d --build >> "$LOG" 2>&1 \
  || { docker compose logs --tail 30 >> "$LOG" 2>&1; fail "docker compose (see $LOG)"; }

# ── 3. host wrapper ─────────────────────────────────────────────────────────
if [ "$WRAPPER" = "1" ]; then
  say "restarting host-wrapper…"
  # Match on the LISTENING socket for port 5501 specifically, so we never kill
  # an unrelated python — including the sibling project's wrapper on 5500.
  pid=$(netstat -ano 2>/dev/null | tr -d '\r' \
        | awk '$2 ~ /:5501$/ && $4 == "LISTENING" {print $5; exit}')
  if [ -n "$pid" ]; then
    taskkill //PID "$pid" //F >> "$LOG" 2>&1 || true
    say "  stopped pid $pid"
  fi

  # Detached, with its own working directory, so it survives this shell exiting.
  ( cd host-wrapper && cmd //c start /min "" cmd //c start.bat >> "$LOG" 2>&1 ) || true

  say "  waiting for the wrapper…"
  up=0
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25; do
    if curl -fs -m 2 http://127.0.0.1:5501/health >/dev/null 2>&1; then up=1; break; fi
    sleep 1
  done
  [ "$up" = "1" ] || fail "wrapper did not come back up. Start it by hand: host-wrapper/start.bat"
fi

# ── 4. verify, do not assume ────────────────────────────────────────────────
say "waiting for the engine…"
up=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if curl -fs -m 3 http://127.0.0.1:8091/api/health >/dev/null 2>&1; then up=1; break; fi
  sleep 1
done
if [ "$up" != "1" ]; then
  docker compose logs --tail 30 engine 2>&1 | tee -a "$LOG"
  fail "engine never became healthy"
fi

# THE check. Health passes on a stale container; a matching SHA does not.
check_sha() {
  name=$1; url=$2
  got=$(curl -fs -m 3 "$url" 2>/dev/null | sed -n 's/.*"sha"[: ]*"\([^"]*\)".*/\1/p')
  if [ -z "$got" ]; then
    say "  $name: no /version endpoint — it is running code older than this check"
    return 1
  fi
  if [ "$got" = "$SHA" ]; then
    say "  $name: $SHORT ✓"
    return 0
  fi
  say "  $name: running $(printf '%s' "$got" | cut -c1-8), expected $SHORT — STALE"
  return 1
}

say "version check:"
stale=0
check_sha "engine " "http://127.0.0.1:8091/api/version" || stale=1
if [ "$WRAPPER" = "1" ]; then
  check_sha "wrapper" "http://127.0.0.1:5501/version" || stale=1
else
  curl -fs -m 3 http://127.0.0.1:5501/health >/dev/null 2>&1 \
    && say "  wrapper: up, NOT restarted (--no-wrapper)" \
    || say "  wrapper: DOWN — start host-wrapper/start.bat"
fi

[ "$stale" = "0" ] || fail "something is not running $SHORT"

# One route that only exists in recent code, as a smoke test beyond the SHA.
count=$(curl -fs -m 3 http://127.0.0.1:8091/api/scenarios 2>/dev/null \
        | grep -o '"blurb"' | wc -l | tr -d ' ')
say "engine serving ${count:-0} scenario(s)"

say "done → http://127.0.0.1:8091  (running $SHORT)"
