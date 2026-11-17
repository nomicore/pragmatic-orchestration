#!/usr/bin/env python3
"""Exit 0 when an OpenCode 2.x raw stream ends with a completed final step.

Completed means: the last step_finish record has reason "stop", and no error
record comes after it. Used by backend_run.sh to keep a finished answer when
the OpenCode process itself exits non-zero.
"""

from __future__ import annotations

import json
import sys


def completed(lines) -> bool:
    done = False
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        typ = obj.get("type")
        part = obj.get("part") if isinstance(obj.get("part"), dict) else {}
        if typ == "error" or obj.get("error"):
            done = False
        elif typ == "step_finish":
            done = part.get("reason") == "stop"
        elif typ in ("step_start", "tool_use"):
            done = False
    return done


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: opencode_completed.py RAW_STREAM", file=sys.stderr)
        return 2
    try:
        with open(sys.argv[1], encoding="utf-8", errors="replace") as f:
            return 0 if completed(f) else 1
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
