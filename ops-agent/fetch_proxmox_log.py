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
        # Deliberately avoids the words "failed"/"error" etc -- this string
        # feeds into run_all.py's anomaly-evidence gate, which scans the
        # whole report for those exact keywords to decide whether to trust
        # an LLM-elevated severity. A "nothing wrong" message that itself
        # contains a trigger word defeats the gate's entire purpose.
        return "(none in the last 30 min)"
    return "\n".join(f"{t.get('type')} (id {t.get('id')}): {t.get('status')}" for t in recent)


def _last_backup_status():
    # PVE's native vzdump->webhook notification is deliberately excluded at the
    # matcher level (type=vzdump) -- this is now the only place backup job
    # health is checked. A failed backup is objectively worth flagging, so it
    # gets a hard floor rather than depending on the LLM to notice it.
    raw = _ssh("pvesh get /nodes/pve/tasks --typefilter vzdump --limit 1 --output-format json")
    try:
        tasks = json.loads(raw)
    except json.JSONDecodeError:
        return "(could not check last backup job status)", "warn"
    if not tasks:
        return "(no backup job history found)", "warn"
    task = tasks[0]
    status = task.get("status", "unknown")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(task.get("starttime", 0)))
    text = f"Last vzdump backup job ({when}): status = {status}"
    floor = "none" if status == "OK" else "critical"
    return text, floor


def fetch():
    journal = _ssh('journalctl -p warning --since "-30 min" --no-pager')
    zpool = _ssh("zpool status -x")
    tasks = _recent_failed_tasks()
    backup_text, backup_floor = _last_backup_status()

    text = (
        "=== Proxmox host: journal warnings (last 30 min) ===\n"
        f"{journal}\n\n"
        "=== Proxmox host: zpool status ===\n"
        f"{zpool}\n\n"
        "=== Proxmox host: task issues in the last 30 min ===\n"
        f"{tasks}\n\n"
        "=== Proxmox host: last backup job ===\n"
        f"{backup_text}\n"
    )
    reason = backup_text if backup_floor != "none" else ""
    return text, backup_floor, reason


if __name__ == "__main__":
    report_text, floor, reason = fetch()
    print(report_text)
    print(f"\n(backup status floor: {floor}; reason: {reason or '(none)'})")
    sys.exit(0)
