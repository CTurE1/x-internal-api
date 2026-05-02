"""Send a Telegram alert when graphql_ops.json drifts.

Diffs HEAD~1 vs HEAD on `data/graphql_ops.json`. If any op's queryId or
method changed (or an op disappeared), posts a single message to the
admin chat. Reads TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT_IDS from env.

Designed for GitHub Actions: run AFTER the auto-commit step. If there
was no diff (no commit), this script silently no-ops.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

import httpx


def _git_show_previous(path: str) -> dict[str, Any] | None:
    """Read the previous version of a file from git. Returns None if no prior version."""
    try:
        out = subprocess.check_output(
            ["git", "show", f"HEAD~1:{path}"],
            stderr=subprocess.STDOUT,
            text=True,
        )
        return json.loads(out)
    except subprocess.CalledProcessError:
        return None
    except json.JSONDecodeError:
        return None


def _load_current(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _diff_ops(prev: dict[str, Any], cur: dict[str, Any]) -> dict[str, list[str]]:
    """Return what changed at the queryId/method/presence level."""
    prev_ops = prev.get("ops", {})
    cur_ops = cur.get("ops", {})

    changed: list[str] = []
    method_changed: list[str] = []
    removed: list[str] = []
    added: list[str] = []

    for name in sorted(set(prev_ops) | set(cur_ops)):
        p = prev_ops.get(name)
        c = cur_ops.get(name)
        if p and not c:
            removed.append(name)
        elif c and not p:
            added.append(name)
        elif p and c:
            if p.get("queryId") != c.get("queryId"):
                changed.append(f"{name}: {p.get('queryId')} -> {c.get('queryId')}")
            if p.get("method") != c.get("method"):
                method_changed.append(f"{name}: {p.get('method')} -> {c.get('method')}")

    return {
        "queryId_changed": changed,
        "method_changed": method_changed,
        "removed": removed,
        "added": added,
    }


def _read_captured_at() -> str:
    """Read timestamp from sidecar file (graphql_ops.json no longer carries it)."""
    try:
        with open("data/captured_at.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            return str(data.get("captured_at", "?"))
    except (OSError, json.JSONDecodeError):
        return "?"


def _format_message(diff: dict[str, list[str]], cur: dict[str, Any]) -> str:
    captured = _read_captured_at()
    count = cur.get("ops_count", "?")
    lines: list[str] = [
        "🔔 <b>x-internal-api drift</b>",
        f"Captured: {captured}",
        f"Ops total: {count}",
        "",
    ]
    if diff["queryId_changed"]:
        lines.append(f"<b>queryId changed ({len(diff['queryId_changed'])}):</b>")
        for s in diff["queryId_changed"][:30]:
            lines.append(f"  • <code>{s}</code>")
        if len(diff["queryId_changed"]) > 30:
            lines.append(f"  … and {len(diff['queryId_changed']) - 30} more")
    if diff["method_changed"]:
        lines.append(f"<b>method changed ({len(diff['method_changed'])}):</b>")
        for s in diff["method_changed"][:10]:
            lines.append(f"  • <code>{s}</code>")
    if diff["removed"]:
        lines.append(f"<b>removed ({len(diff['removed'])}):</b> {', '.join(diff['removed'][:20])}")
    if diff["added"]:
        lines.append(f"<b>added ({len(diff['added'])}):</b> {', '.join(diff['added'][:20])}")
    return "\n".join(lines)


def _send_telegram(token: str, chat_ids: list[str], text: str) -> None:
    for chat_id in chat_ids:
        try:
            r = httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=15.0,
            )
            r.raise_for_status()
        except Exception as e:
            print(f"warn: telegram send to {chat_id} failed: {e}", file=sys.stderr)


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids_csv = os.getenv("TELEGRAM_ADMIN_CHAT_IDS", "").strip()
    if not token or not chat_ids_csv:
        print("info: telegram secrets not configured, skipping alert", file=sys.stderr)
        return 0
    chat_ids = [c.strip() for c in chat_ids_csv.split(",") if c.strip()]

    path = "data/graphql_ops.json"
    cur = _load_current(path)
    prev = _git_show_previous(path)
    if prev is None:
        print("info: no previous version, skipping alert (initial commit)", file=sys.stderr)
        return 0

    diff = _diff_ops(prev, cur)
    if not any(diff.values()):
        print("info: no drift, skipping alert", file=sys.stderr)
        return 0

    msg = _format_message(diff, cur)
    _send_telegram(token, chat_ids, msg)
    print(f"info: alert sent to {len(chat_ids)} chat(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
