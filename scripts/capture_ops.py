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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


# x.com/ root no longer serves the legacy React shell — since ~2026-07 it
# returns a lightweight "x-web" SSR page whose only script is
# entry-client-logged-out-<hash>.js (no GraphQL manifest, no chunk map).
# The responsive-web shell that carries the manifest is still served on the
# routes below; we take the first one that yields a main.<hash>.js.
X_SHELL_URLS = (
    "https://x.com/i/flow/login",
    "https://x.com/home",
    "https://x.com/settings/account",
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)
# Lazily-loaded chunks that never carry ops — skipping them cuts the sweep
# roughly in half (translations and icon sprites are the bulk of the map).
_SKIP_CHUNK_RE = re.compile(r"^(?:i18n/|icons\.)|emoji-|countries-")
CHUNK_CONCURRENCY = 16

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

_MAIN_BUNDLE_RE = re.compile(
    r"https://abs\.twimg\.com/responsive-web/client-web/main\.[A-Za-z0-9]+\.js"
)
# Chunk URLs are not written out anywhere — the inline webpack runtime in the
# shell HTML rebuilds them from two id-keyed maps:
#   p.u = e => ((({346:"bundle.NotABot",…})[e] || e) + "." +
#               ({346:"13fff73",…})[e] + "a.js")
# so we mirror that composition here. Most ops (HomeTimeline, Communities*, …)
# live in those lazy chunks, not in main.js.
_CHUNK_HASH_MAP_RE = re.compile(r'\{(?:"?\d{2,7}"?:"[a-z0-9]{6,10}",){10,}[^{}]*\}')
_CHUNK_SUFFIX_RE = re.compile(r'\)\[\w\]\s*\+\s*"([A-Za-z0-9_\-]*\.js)"')
_PUBLIC_PATH_RE = re.compile(r'\.p\s*=\s*"(https://abs\.twimg\.com/[^"]+/)"')
_MAP_ENTRY_RE = re.compile(r'"?(\d{2,7})"?:"([^"]+)"')


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


def _fetch_shell(client: httpx.Client) -> tuple[str, str]:
    """Fetch a page still served by the legacy responsive-web shell.

    Returns (html, main_js_url). Raises when no candidate route carries the
    shell — that means X moved the bundle again and the regexes need a look.
    """
    tried: list[str] = []
    for url in X_SHELL_URLS:
        resp = client.get(url)
        resp.raise_for_status()
        main_match = _MAIN_BUNDLE_RE.search(resp.text)
        if main_match:
            return resp.text, main_match.group(0)
        tried.append(f"{url} (HTTP {resp.status_code}, {len(resp.text)}B)")
    raise RuntimeError(
        "main.<hash>.js URL not found on any X.com shell route: " + ", ".join(tried)
    )


def _chunk_urls(shell_html: str) -> list[str]:
    """Rebuild every lazy-chunk URL from the inline webpack runtime maps."""
    hash_match = _CHUNK_HASH_MAP_RE.search(shell_html)
    if not hash_match:
        raise RuntimeError("webpack chunk-hash map not found in shell HTML")
    hashes = dict(_MAP_ENTRY_RE.findall(hash_match.group(0)))

    # The name map is the object literal immediately before the hash map,
    # inside the same `.u=` arrow function.
    head = shell_html[: hash_match.start()]
    u_idx = head.rfind(".u=")
    if u_idx == -1:
        raise RuntimeError("webpack chunk-name map not found in shell HTML")
    names = dict(_MAP_ENTRY_RE.findall(head[u_idx:]))

    suffix_match = _CHUNK_SUFFIX_RE.search(shell_html, hash_match.end())
    suffix = suffix_match.group(1) if suffix_match else "a.js"
    path_match = _PUBLIC_PATH_RE.search(shell_html)
    base = path_match.group(1) if path_match else (
        "https://abs.twimg.com/responsive-web/client-web/"
    )

    urls = set()
    for chunk_id, digest in hashes.items():
        name = names.get(chunk_id, chunk_id)  # webpack falls back to the raw id
        if _SKIP_CHUNK_RE.search(name):
            continue
        urls.add(f"{base}{name}.{digest}{suffix}")
    return sorted(urls)


def _fetch_text(client: httpx.Client, url: str) -> str:
    resp = client.get(url)
    resp.raise_for_status()
    return resp.text


def _parse_chunks(client: httpx.Client, urls: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch chunks concurrently and merge the ops each one declares.

    Chunks are parsed in the worker so the ~40 MB of JS never piles up in
    memory. A chunk that 404s (stale map entry) is skipped, not fatal.
    """
    def _get_ops(url: str) -> dict[str, dict[str, Any]]:
        try:
            return _parse_ops(_fetch_text(client, url))
        except Exception as e:
            print(f"warn: failed to fetch {url}: {e}", file=sys.stderr)
            return {}

    ops: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=CHUNK_CONCURRENCY) as pool:
        for found in pool.map(_get_ops, urls):  # ordered by input → deterministic
            ops.update(found)
    return ops


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
        shell_html, main_url = _fetch_shell(client)
        main_js = _fetch_text(client, main_url)
        chunk_urls = _chunk_urls(shell_html)
        print(f"sweeping {len(chunk_urls)} chunks", file=sys.stderr)
        chunk_ops = _parse_chunks(client, chunk_urls)

    ops: dict[str, dict[str, Any]] = {}
    ops.update(_parse_ops(main_js))
    ops.update(chunk_ops)

    if not ops:
        print("error: zero ops captured", file=sys.stderr)
        return 2

    captured_at = datetime.now(timezone.utc).isoformat()
    ops_path = args.output_dir / "graphql_ops.json"
    ts_path = args.output_dir / "captured_at.txt"
    ts_json_path = args.output_dir / "captured_at.json"

    # captured_at is intentionally NOT in graphql_ops.json. Timestamp lives in
    # data/captured_at.{txt,json}. Keeping it out of the main payload means
    # `git diff` only fires when ops actually changed — no every-6h commit
    # noise + no false-positive drift alerts.
    payload = {
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
