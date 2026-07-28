#!/usr/bin/env sh
# Point git at the versioned hooks in tools/hooks.
#
# core.hooksPath rather than copying into .git/hooks: copies go stale silently
# the moment someone edits the original, and nobody notices for weeks.
set -e
cd "$(dirname "$0")/.."
chmod +x tools/hooks/* tools/redeploy.sh 2>/dev/null || true
git config core.hooksPath tools/hooks
echo "hooks installed: $(git config core.hooksPath)"
echo "  post-commit -> tools/redeploy.sh --quiet (background)"
echo "  skip once with: SKIP_DEPLOY=1 git commit -m '...'"
echo "  disable with:   git config --unset core.hooksPath"
