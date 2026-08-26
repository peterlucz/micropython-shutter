#!/usr/bin/env python3
"""Persistent, append-only history of every ops-triage run's outcome -- so
Peter (or either LLM in the pipeline) can check recent alert activity without
copy-pasting phone notifications back into a chat session, and so triage.py /
verify_with_cloud.py can see whether a candidate issue is a one-off or a
recurring pattern instead of judging each run in isolation.

Usage: ./alert_log.py [--hours N]   (prints a digest of the last N hours, default 24)
       import as: alert_log.record(...); alert_log.recent_history_text(hours=24) -> str
"""
import argparse
import datetime
import json
import pathlib
import time

LOG_FILE = pathlib.Path(__file__).parent / "alert_log.jsonl"


def record(severity: str, summary: str, floors: dict, llm_severity: str = "",
           cloud_verdict: str = "", notified: bool = False):
    entry = {
        "ts": time.time(),
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
        "severity": severity,
        "llm_severity": llm_severity,
        "cloud_verdict": cloud_verdict,  # "confirmed" / "denied" / "" (not called)
        "notified": notified,
        "summary": summary,
        "floors": floors,  # e.g. {"host": "none", "immich": "warn", ...}
    }
    # Append-only (one JSON object per line) rather than read-modify-write a
    # single JSON blob every 20 min -- this file only ever grows, and a plain
    # append can't corrupt earlier history if a run is killed mid-write.
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _load(hours: float):
    if not LOG_FILE.exists():
        return []
    cutoff = time.time() - hours * 3600
    entries = []
    for line in LOG_FILE.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("ts", 0) >= cutoff:
            entries.append(entry)
    return entries


def _condition_key(entry: dict) -> str:
    # Same grouping granularity run_all.py's repeat-notification throttle
    # uses -- narrative-only alerts (all floors "none") aren't individually
    # fingerprinted by content, so two different narrative issues in that
    # state group together. Documented limitation, not fixed here either.
    floors = entry.get("floors", {})
    floor_part = "|".join(f"{k}={v}" for k, v in sorted(floors.items()))
    return f"severity={entry.get('severity')}|{floor_part}"


def recent_history_text(hours: float = 24) -> str:
    """Compact, LLM-readable digest of alert activity in the lookback window
    -- fed into triage.py's and verify_with_cloud.py's prompts so both models
    can see whether something is a fresh finding or a known/recurring one."""
    entries = _load(hours)
    if not entries:
        return f"No ops-triage history in the last {hours:.0f}h (first run, or log not yet populated)."

    groups = {}
    for entry in entries:
        key = _condition_key(entry)
        group = groups.setdefault(key, {
            "count": 0, "first": entry["time"], "last": entry["time"],
            "summary": entry["summary"], "notified": 0,
        })
        group["count"] += 1
        group["last"] = entry["time"]
        if entry.get("notified"):
            group["notified"] += 1

    non_none = {k: g for k, g in groups.items() if not k.startswith("severity=none|")}
    lines = [f"Recent ops-triage activity (last {hours:.0f}h, {len(entries)} runs logged):"]
    if not non_none:
        lines.append("- all clear the entire window (every run was severity=none)")
    else:
        for group in sorted(non_none.values(), key=lambda g: g["count"], reverse=True):
            lines.append(
                f'- "{group["summary"]}" -- seen {group["count"]}x '
                f'(first {group["first"]}, last {group["last"]}), notified {group["notified"]}x'
            )
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=24)
    args = parser.parse_args()
    print(recent_history_text(args.hours))
