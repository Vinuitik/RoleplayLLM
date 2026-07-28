"""Where did a build session's wall clock actually go?

Claude Code writes a JSONL transcript per session, one line per message, each
stamped. That is the only honest record of pacing — memory of a six-hour session
is worthless, and "it felt slow" points at the wrong thing more often than not.

    python tools/session_time.py                 # this project, latest session
    python tools/session_time.py --all           # every session for this project
    python tools/session_time.py --file X.jsonl

The number that matters is usually NOT generation time. It is the gap between an
assistant turn ending and the next user message — the conversational round trip.
If that dominates, the fix is fewer and richer turns, not faster tooling.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import pathlib
import sys

# Claude Code slugifies the project path into a directory name.
PROJECTS = pathlib.Path(os.path.expanduser("~/.claude/projects"))


def transcripts(project_dir: pathlib.Path) -> list[pathlib.Path]:
    return sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)


def load(path: pathlib.Path) -> list[tuple[datetime.datetime, dict]]:
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        stamp = entry.get("timestamp")
        if stamp:
            rows.append((datetime.datetime.fromisoformat(
                stamp.replace("Z", "+00:00")), entry))
    rows.sort(key=lambda r: r[0])
    return rows


def report(path: pathlib.Path) -> None:
    rows = load(path)
    if len(rows) < 2:
        print(f"{path.name}: too short to analyse")
        return

    span = (rows[-1][0] - rows[0][0]).total_seconds()
    print(f"\n=== {path.name} ===")
    print(f"{rows[0][0].astimezone():%H:%M} -> {rows[-1][0].astimezone():%H:%M}"
          f"   {span/3600:.2f} h   {len(rows)} messages")

    # Attribute every gap to the transition that produced it. `assistant -> user`
    # is the round trip: me finishing, then waiting. It is usually the largest
    # bucket by far, and it is the one nobody suspects.
    buckets: collections.Counter = collections.Counter()
    longest = []
    for i in range(1, len(rows)):
        delta = (rows[i][0] - rows[i - 1][0]).total_seconds()
        key = f"{rows[i-1][1].get('type')} -> {rows[i][1].get('type')}"
        buckets[key] += delta
        longest.append((delta, key, rows[i][0]))

    print("\nwall clock by transition:")
    for key, seconds in buckets.most_common(6):
        print(f"  {seconds/60:7.1f} min  ({seconds/span:5.1%})  {key}")

    longest.sort(reverse=True)
    print("\nlongest single gaps:")
    for delta, key, when in longest[:8]:
        print(f"  {delta/60:6.1f} min  {when.astimezone():%H:%M}  {key}")

    tools: collections.Counter = collections.Counter()
    blocked = 0
    for _stamp, entry in rows:
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tools[block.get("name")] += 1
            elif block.get("type") == "tool_result":
                blob = json.dumps(block)[:400].lower()
                if "permission" in blob or "denied" in blob:
                    blocked += 1

    print(f"\ntool calls: {sum(tools.values())}   permission blocks: {blocked}")
    print("  " + ", ".join(f"{name} {n}" for name, n in tools.most_common()))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="a specific .jsonl transcript")
    parser.add_argument("--all", action="store_true",
                        help="every session for this project, not just the latest")
    args = parser.parse_args()

    if args.file:
        report(pathlib.Path(args.file))
        return 0

    if not PROJECTS.exists():
        print(f"no transcripts at {PROJECTS}", file=sys.stderr)
        return 1

    # Match this repo's slug: the cwd path with separators replaced by dashes.
    slug = str(pathlib.Path.cwd()).replace(":", "-").replace("\\", "-").replace("/", "-")
    candidates = [d for d in PROJECTS.iterdir()
                  if d.is_dir() and d.name.lower().endswith(slug.lower().lstrip("-"))]
    if not candidates:
        candidates = [d for d in PROJECTS.iterdir() if d.is_dir()]
        print("(no exact project match; showing all projects)")

    for project_dir in candidates:
        found = transcripts(project_dir)
        for path in (found if args.all else found[-1:]):
            report(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
