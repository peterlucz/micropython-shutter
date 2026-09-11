#!/usr/bin/env python3
"""Fetch health signals from the Proxmox host, VM100 (HAOS), VM101 (desktop),
Immich (VM102), and this LXC itself; triage with the local LLM; notify if
warranted.

Entry point for the ops-triage systemd timer. Run manually with: ./run_all.py
"""
import datetime
import json
import pathlib
import re
import sys
import time

import alert_log
import fetch_proxmox_log
import fetch_immich_log
import fetch_self_log
import fetch_vm_log
import notify
import triage
import verify_with_cloud

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

# Symmetric to ANOMALY_PATTERN above, but in the opposite direction: some
# conditions are already fully explained and explicitly labeled benign by the
# fetch layer (see fetch_immich_log._summarize_postgres_log), yet the local
# 3B model has repeatedly still narrated them as alarming ("impacting asset
# processing tasks") and assigned "warn" on its own -- confirmed via
# alert_log.jsonl, 7 real pages 2026-09-08/09, every one with
# floors["immich"]=="none" (the deterministic checks correctly saw nothing
# wrong). ANOMALY_PATTERN alone doesn't stop this: it checks the *entire*
# combined report for corroboration, so an unrelated keyword anywhere else
# (host/vm100/vm101/self) can "corroborate" a narrative that has nothing to
# do with that other section.
#
# ORIGINALLY this was a regex matched against the LLM's own summary text
# (name the narrative, refuse to let it drive severity). Retired 2026-09-11:
# alert_log.jsonl showed multiple "warn" pages with cloud_verdict="confirmed"
# for this exact already-explained condition (immich_floor=="none" the whole
# time) -- the regex was checked against wording that varies both by local-
# model phrasing AND by the cloud-verification rewrite (see verify_with_cloud
# .py, which replaces `summary` with its own text on confirm), so it could
# and did miss real instances. `fetch_immich_log.fetch()` now returns a plain
# boolean (`immich_known_benign`) from the one place that actually knows the
# ground truth, checked below -- robust to any amount of downstream
# rewording, and (since it's applied before severity even reaches
# ALERT_LEVELS) skips the cloud-verification call entirely for this
# condition rather than hoping to catch it after the fact.

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


def _recently_notified(condition_key: str) -> bool:
    """Read-only peek at whether this exact condition was already notified
    within the throttle window -- doesn't record anything. Used to skip a
    cloud verification call entirely for an already-throttled recurring
    narrative issue: whatever the cloud model says, _should_notify() below
    will suppress the notification either way, so there's nothing to gain
    from spending an API call (and its latency) on an outcome the user
    won't see."""
    state = _load_state()
    last = state.get(condition_key)
    return last is not None and time.time() - last < RENOTIFY_INTERVAL_S


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

    host_text, host_floor, host_reason = fetch_proxmox_log.fetch()
    immich_text, immich_floor, immich_reason, immich_known_benign = fetch_immich_log.fetch()
    vm100_text, vm100_floor, vm100_reason = fetch_vm_log.fetch("100", "HAOS", "/mnt/data")
    vm101_text, vm101_floor, vm101_reason = fetch_vm_log.fetch("101", "Desktop")
    self_text, self_floor, self_reason = fetch_self_log.fetch()

    report = "\n".join((host_text, immich_text, vm100_text, vm101_text, self_text))
    history_text = alert_log.recent_history_text(hours=24)
    llm_severity, llm_summary = triage.triage(report, history_text)

    hard_floor = max(
        host_floor, immich_floor, vm100_floor, vm101_floor, self_floor,
        key=SEVERITY_ORDER.index,
    )
    floor_reasons = "; ".join(r for r in (host_reason, immich_reason, vm100_reason, vm101_reason, self_reason) if r)

    if SEVERITY_ORDER.index(llm_severity) > SEVERITY_ORDER.index(hard_floor) and not ANOMALY_PATTERN.search(report):
        llm_summary = f"(capped: no corroborating evidence in raw data for '{llm_severity}') {llm_summary}"
        llm_severity = hard_floor

    if immich_known_benign and immich_floor == "none":
        llm_summary = f"(capped: known-benign Immich duplicate-checksum narrative) {llm_summary}"
        llm_severity = hard_floor

    severity = max(llm_severity, hard_floor, key=SEVERITY_ORDER.index)

    # When a hard floor is at or above the LLM's own read, it's the floor that
    # actually explains the problem -- use its concrete reason as the message
    # body instead of the LLM's (possibly irrelevant or fabricated) narrative.
    floor_driven = bool(floor_reasons) and SEVERITY_ORDER.index(hard_floor) >= SEVERITY_ORDER.index(llm_severity)
    summary = floor_reasons if floor_driven else llm_summary

    print(
        f"[{now}] severity={severity} (llm={llm_severity}, host={host_floor}, immich={immich_floor}, "
        f"vm100={vm100_floor}, vm101={vm101_floor}, self={self_floor}) summary={summary}"
    )

    cloud_verdict = ""
    if severity in ALERT_LEVELS and not floor_driven:
        pending_condition_key = (
            f"severity={severity}|host={host_floor}|immich={immich_floor}|"
            f"vm100={vm100_floor}|vm101={vm101_floor}|self={self_floor}"
        )
        if _recently_notified(pending_condition_key):
            print(f"[{now}] skipping cloud verification: '{summary}' already notified recently, would be suppressed anyway")
        else:
            # Floor-driven alerts are already deterministic (container down,
            # backup failed, disk genuinely over threshold) -- no need to
            # spend an API call double-checking those. This branch is
            # specifically the local 3B model's own narrative judgment,
            # which has repeatedly been unreliable even after passing the
            # anomaly-evidence gate above. Get a second opinion from a more
            # capable cloud model before paging the phone; only proceed if
            # it agrees this is real.
            try:
                confirmed, cloud_summary = verify_with_cloud.verify(report, severity, summary, history_text)
                cloud_verdict = "confirmed" if confirmed else "denied"
                print(f"[{now}] cloud verification: {cloud_verdict} - {cloud_summary}")
                if confirmed:
                    summary = cloud_summary
                else:
                    severity = "none"
                    summary = f"(suppressed: cloud model disagreed) {cloud_summary}"
            except Exception as e:
                print(f"[{now}] cloud verification failed ({e}), proceeding with local read")

    notified = False
    if severity in ALERT_LEVELS:
        condition_key = (
            f"severity={severity}|host={host_floor}|immich={immich_floor}|"
            f"vm100={vm100_floor}|vm101={vm101_floor}|self={self_floor}"
        )
        if _should_notify(condition_key):
            title = "Ops-agent: CRITICAL" if severity == "critical" else "Ops-agent"
            notify.notify(title, summary, severity)
            notified = True
            print(f"[{now}] notified (severity={severity})")
        else:
            print(f"[{now}] suppressed repeat notification (severity={severity}, already notified recently)")
    else:
        # Condition resolved (or never triggered) -- clear any stale state so
        # a future recurrence isn't throttled by an old timestamp.
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        print(f"[{now}] no notification (severity={severity})")

    alert_log.record(
        severity=severity,
        summary=summary,
        floors={"host": host_floor, "immich": immich_floor, "vm100": vm100_floor,
                "vm101": vm101_floor, "self": self_floor},
        llm_severity=llm_severity,
        cloud_verdict=cloud_verdict,
        notified=notified,
    )


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
