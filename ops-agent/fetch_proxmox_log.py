#!/usr/bin/env python3
"""Pull recent warning-level signals from the Proxmox host itself.

Usage: ./fetch_proxmox_log.py        (prints report to stdout)
       import as fetch_proxmox_log; fetch_proxmox_log.fetch()
"""
import json
import subprocess
import sys
import time

SSH_HOST = "proxmox"
TASK_LOOKBACK_S = 30 * 60


def _ssh(cmd):
    result = subprocess.run(["ssh", SSH_HOST, cmd], capture_output=True, text=True, timeout=30)
    return result.stdout.strip() or "(no output)"


def _recent_failed_tasks():
    # `pvesh ... tasks --errors 1` returns *historical* failed/warned tasks with no
    # time bound of its own -- without filtering, the LLM would see weeks-old,
    # already-resolved failures on every run and could re-flag them as current.
    raw = _ssh("pvesh get /nodes/pve/tasks --errors 1 --limit 20 --output-format json")
    try:
        tasks = json.loads(raw)
    except json.JSONDecodeError:
        return raw  # fall back to raw output rather than hiding a parse failure
    cutoff = time.time() - TASK_LOOKBACK_S
    recent = [t for t in tasks if t.get("starttime", 0) >= cutoff]
    if not recent:
        return "(no failed/warned tasks in the last 30 min)"
    return "\n".join(f"{t.get('type')} (id {t.get('id')}): {t.get('status')}" for t in recent)


def fetch():
    journal = _ssh('journalctl -p warning --since "-30 min" --no-pager')
    zpool = _ssh("zpool status -x")
    tasks = _recent_failed_tasks()

    return (
        "=== Proxmox host: journal warnings (last 30 min) ===\n"
        f"{journal}\n\n"
        "=== Proxmox host: zpool status ===\n"
        f"{zpool}\n\n"
        "=== Proxmox host: failed/warned tasks in the last 30 min ===\n"
        f"{tasks}\n"
    )


if __name__ == "__main__":
    print(fetch())
    sys.exit(0)
