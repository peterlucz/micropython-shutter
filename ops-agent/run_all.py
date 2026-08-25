#!/usr/bin/env python3
"""Fetch Proxmox host + Immich (VM102) signals, triage with the local LLM, notify if warranted.

Entry point for the ops-triage systemd timer. Run manually with: ./run_all.py
"""
import datetime
import sys

import fetch_proxmox_log
import fetch_immich_log
import notify
import triage

ALERT_LEVELS = {"warn", "critical"}
SEVERITY_ORDER = ["none", "info", "warn", "critical"]


def main():
    now = datetime.datetime.now().isoformat(timespec="seconds")
    print(f"[{now}] ops-triage run starting")

    proxmox_text, backup_floor = fetch_proxmox_log.fetch()
    immich_text, container_floor = fetch_immich_log.fetch()
    report = proxmox_text + "\n" + immich_text
    llm_severity, summary = triage.triage(report)

    # Take the most severe of the LLM's read and the deterministic floors --
    # a stopped container or a failed backup job shouldn't depend on the 3B
    # model's judgment call to get flagged.
    hard_floor = max(container_floor, backup_floor, key=SEVERITY_ORDER.index)
    severity = max(llm_severity, hard_floor, key=SEVERITY_ORDER.index)
    if severity != llm_severity:
        summary = f"{summary} [hard floor: {severity} (container={container_floor}, backup={backup_floor})]"

    print(
        f"[{now}] severity={severity} (llm={llm_severity}, container_floor={container_floor}, "
        f"backup_floor={backup_floor}) summary={summary}"
    )

    if severity in ALERT_LEVELS:
        title = "Ops-agent: CRITICAL" if severity == "critical" else "Ops-agent"
        notify.notify(title, summary, severity)
        print(f"[{now}] notified (severity={severity})")
    else:
        print(f"[{now}] no notification (severity={severity})")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # A broken pipeline is itself worth knowing about, not just going silent.
        print(f"ERROR: ops-triage run failed: {e}", file=sys.stderr)
        try:
            notify.notify("Ops-agent error", f"ops-triage run failed: {e}", "warn")
        except Exception:
            pass
        sys.exit(1)
