#!/usr/bin/env python3
"""Fetch Proxmox host + Immich (VM102) signals, triage with the local LLM, notify if warranted.

Entry point for the ops-triage systemd timer. Run manually with: ./run_all.py
"""
import datetime
import json
import pathlib
import re
import sys
import time

import fetch_proxmox_log
import fetch_immich_log
import notify
import triage

ALERT_LEVELS = {"warn", "critical"}
SEVERITY_ORDER = ["none", "info", "warn", "critical"]

# Observed in practice: the 3B model will fabricate a "warn" out of thin air
# even when a container's log fetch came back completely empty (no evidence
# of anything at all). Don't let the LLM's own severity read escalate above
# the deterministic floor unless something in the raw data actually backs it
# up -- this is a corroboration check, not a rewording of the floor logic.
ANOMALY_PATTERN = re.compile(
    r"\b(error|exception|traceback|fail(?:ed|ing)?|critical|panic|corrupt(?:ed)?|denied|"
    r"refused|crash(?:ed|ing)?|oom|out of memory|segfault|unable to|cannot connect|"
    r"timed? ?out|unhealthy|unreachable|degraded)\b",
    re.IGNORECASE,
)

# A genuinely unresolved problem (e.g. a real backup failure) shouldn't page
# every single 20-min cycle forever -- re-remind at most this often per
# distinct condition, tracked in STATE_FILE across runs.
RENOTIFY_INTERVAL_S = 4 * 60 * 60
STATE_FILE = pathlib.Path(__file__).parent / "state.json"


def _load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _should_notify(condition_key: str) -> bool:
    """True if this exact condition hasn't been notified recently. Resets
    itself (via main()'s cleanup below) as soon as the condition resolves, so
    a *future* recurrence of the same condition notifies fresh rather than
    staying throttled forever."""
    state = _load_state()
    last = state.get(condition_key)
    now = time.time()
    if last is not None and now - last < RENOTIFY_INTERVAL_S:
        return False
    state[condition_key] = now
    STATE_FILE.write_text(json.dumps(state))
    return True


def main():
    now = datetime.datetime.now().isoformat(timespec="seconds")
    print(f"[{now}] ops-triage run starting")

    proxmox_text, backup_floor, backup_reason = fetch_proxmox_log.fetch()
    immich_text, container_floor, immich_reason = fetch_immich_log.fetch()
    report = proxmox_text + "\n" + immich_text
    llm_severity, llm_summary = triage.triage(report)

    hard_floor = max(container_floor, backup_floor, key=SEVERITY_ORDER.index)
    floor_reasons = "; ".join(r for r in (backup_reason, immich_reason) if r)

    if SEVERITY_ORDER.index(llm_severity) > SEVERITY_ORDER.index(hard_floor) and not ANOMALY_PATTERN.search(report):
        llm_summary = f"(capped: no corroborating evidence in raw data for '{llm_severity}') {llm_summary}"
        llm_severity = hard_floor

    severity = max(llm_severity, hard_floor, key=SEVERITY_ORDER.index)

    # When a hard floor is at or above the LLM's own read, it's the floor that
    # actually explains the problem -- use its concrete reason as the message
    # body instead of the LLM's (possibly irrelevant or fabricated) narrative.
    if floor_reasons and SEVERITY_ORDER.index(hard_floor) >= SEVERITY_ORDER.index(llm_severity):
        summary = floor_reasons
    else:
        summary = llm_summary

    print(
        f"[{now}] severity={severity} (llm={llm_severity}, container_floor={container_floor}, "
        f"backup_floor={backup_floor}) summary={summary}"
    )

    if severity in ALERT_LEVELS:
        condition_key = f"severity={severity}|container={container_floor}|backup={backup_floor}"
        if _should_notify(condition_key):
            title = "Ops-agent: CRITICAL" if severity == "critical" else "Ops-agent"
            notify.notify(title, summary, severity)
            print(f"[{now}] notified (severity={severity})")
        else:
            print(f"[{now}] suppressed repeat notification (severity={severity}, already notified recently)")
    else:
        # Condition resolved (or never triggered) -- clear any stale state so
        # a future recurrence isn't throttled by an old timestamp.
        if STATE_FILE.exists():
            STATE_FILE.unlink()
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
