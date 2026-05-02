"""Validate data/graphql_ops.json against expected shape.

Used by the refresh workflow to abort on malformed captures (bad parse,
network glitch). Bad captures must NOT be committed because the JSON is
the canonical contract for downstream consumers.

Exit codes:
  0  ok
  1  schema violation (bad JSON shape)
  2  insufficient ops (capture likely failed mid-flight)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

MIN_OPS_COUNT = 80     # below this is suspicious — sanity check
REQUIRED_OPS = [
    # Critical ops a real capture MUST contain.
    "HomeTimeline", "TweetDetail", "CreateTweet", "UserByScreenName",
    "FavoriteTweet", "UnfavoriteTweet",
]


def main() -> int:
    path = Path("data/graphql_ops.json")
    if not path.exists():
        print(f"error: {path} missing", file=sys.stderr)
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        return 1

    if not isinstance(data, dict):
        print("error: top-level not a dict", file=sys.stderr)
        return 1
    for key in ("ops_count", "ops"):
        if key not in data:
            print(f"error: missing top-level key: {key}", file=sys.stderr)
            return 1
    ops = data["ops"]
    if not isinstance(ops, dict):
        print("error: 'ops' is not a dict", file=sys.stderr)
        return 1
    if data["ops_count"] != len(ops):
        print(
            f"error: ops_count={data['ops_count']} but len(ops)={len(ops)}",
            file=sys.stderr,
        )
        return 1
    if len(ops) < MIN_OPS_COUNT:
        print(f"error: only {len(ops)} ops captured (min {MIN_OPS_COUNT})", file=sys.stderr)
        return 2

    missing = [name for name in REQUIRED_OPS if name not in ops]
    if missing:
        print(f"error: required ops missing: {missing}", file=sys.stderr)
        return 1

    for name, op in ops.items():
        for k in ("queryId", "operationName", "operationType", "method"):
            if k not in op:
                print(f"error: op {name} missing key {k}", file=sys.stderr)
                return 1
        if op["method"] not in ("GET", "POST"):
            print(f"error: op {name} has bad method {op['method']!r}", file=sys.stderr)
            return 1

    print(f"ok: {len(ops)} ops, schema valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
