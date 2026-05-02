"""Capture X.com GraphQL operations from the public web bundle.

Standalone — no project dependencies beyond httpx. Outputs:
- data/graphql_ops.json    (canonical: dict[operationName] -> op record)
- data/captured_at.txt     (ISO-8601 UTC, human readable)
- data/captured_at.json    ({"captured_at": "...", "ops_count": N})

Usage:
    python scripts/capture_ops.py
    python scripts/capture_ops.py --output-dir data
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


X_HOME = "https://x.com/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)

# Regex: api={...api manifest object...} embedded in main bundle.
# We match a JS object literal whose keys are stable shapes seen across
# X.com bundle revisions: queryId/operationName/operationType.
_API_BLOCK_RE = re.compile(
    r"\{queryId:\s*\"([A-Za-z0-9_\-]+)\","
    r"operationName:\s*\"([A-Za-z0-9_]+)\","
    r"operationType:\s*\"(query|mutation|subscription)\","
    r"metadata:\s*\{([^}]*?)\}\}"
)
_FEATURE_SWITCHES_RE = re.compile(r"featureSwitches:\s*\[([^\]]*)\]")
_FIELD_TOGGLES_RE = re.compile(r"fieldToggles:\s*\[([^\]]*)\]")
_QUOTED_NAME_RE = re.compile(r"\"([A-Za-z0-9_]+)\"")


def _build_client() -> httpx.Client:
    return httpx.Client(
        headers={
            "user-agent": USER_AGENT,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
        },
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
    )


def _fetch_main_bundle(client: httpx.Client) -> str:
    """Fetch X.com home → extract main.<hash>.js URL → fetch JS contents."""
    resp = client.get(X_HOME)
    resp.raise_for_status()
    main_match = re.search(
        r"https://abs\.twimg\.com/responsive-web/client-web/main\.[a-f0-9]+\.js",
        resp.text,
    )
    if not main_match:
        raise RuntimeError("main.<hash>.js URL not found in X.com home HTML")
    main_url = main_match.group(0)

    js_resp = client.get(main_url)
    js_resp.raise_for_status()
    return js_resp.text


def _fetch_ondemand_chunks(client: httpx.Client, main_js: str) -> list[str]:
    """Find ondemand chunk references in main.js and fetch them.

    Many GraphQL ops are split into lazily-loaded chunks. We capture all
    https://abs.twimg.com/.../ondemand.<id>.js URLs and pull each.
    """
    chunk_urls = sorted(set(re.findall(
        r"https://abs\.twimg\.com/responsive-web/client-web/ondemand\.[\w\.]+\.js",
        main_js,
    )))
    contents = []
    for url in chunk_urls[:60]:  # cap to keep run < 30s
        try:
            r = client.get(url)
            r.raise_for_status()
            contents.append(r.text)
        except Exception as e:
            print(f"warn: failed to fetch {url}: {e}", file=sys.stderr)
    return contents


def _parse_ops(js_text: str) -> dict[str, dict[str, Any]]:
    """Extract every GraphQL op manifest from a JS blob."""
    ops: dict[str, dict[str, Any]] = {}
    for match in _API_BLOCK_RE.finditer(js_text):
        query_id, op_name, op_type, metadata_block = match.groups()
        feature_switches: list[str] = []
        field_toggles: list[str] = []
        fs = _FEATURE_SWITCHES_RE.search(metadata_block)
        if fs:
            feature_switches = _QUOTED_NAME_RE.findall(fs.group(1))
        ft = _FIELD_TOGGLES_RE.search(metadata_block)
        if ft:
            field_toggles = _QUOTED_NAME_RE.findall(ft.group(1))
        ops[op_name] = {
            "queryId": query_id,
            "operationName": op_name,
            "operationType": op_type,
            "method": "POST" if op_type == "mutation" else "GET",
            "featureSwitches": feature_switches,
            "fieldToggles": field_toggles,
        }
    return ops


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    with _build_client() as client:
        main_js = _fetch_main_bundle(client)
        chunk_blobs = _fetch_ondemand_chunks(client, main_js)

    ops: dict[str, dict[str, Any]] = {}
    ops.update(_parse_ops(main_js))
    for blob in chunk_blobs:
        ops.update(_parse_ops(blob))

    if not ops:
        print("error: zero ops captured", file=sys.stderr)
        return 2

    captured_at = datetime.now(timezone.utc).isoformat()
    ops_path = args.output_dir / "graphql_ops.json"
    ts_path = args.output_dir / "captured_at.txt"
    ts_json_path = args.output_dir / "captured_at.json"

    payload = {
        "captured_at": captured_at,
        "ops_count": len(ops),
        "ops": dict(sorted(ops.items())),
    }
    ops_path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    ts_path.write_text(captured_at + "\n", encoding="utf-8")
    ts_json_path.write_text(
        json.dumps({"captured_at": captured_at, "ops_count": len(ops)}, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"captured {len(ops)} ops at {captured_at}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
