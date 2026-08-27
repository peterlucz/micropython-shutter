#!/usr/bin/env python3
"""Pull container health, recent logs, disk usage, and RAM for Immich (VM102).

Goes LXC -> ssh proxmox -> `qm guest exec 102`, since VM102 has no direct
SSH access set up.

Usage: ./fetch_immich_log.py
"""
import re

import host_metrics

VMID = "102"
CONTAINERS = ["immich_server", "immich_postgres", "immich_machine_learning", "immich_redis"]
SEVERITY_ORDER = host_metrics.SEVERITY_ORDER


def _summarize_redis_log(raw_log: str) -> str:
    # Redis forks a new child process (new PID) for every periodic RDB
    # snapshot -- normal, healthy, happens every ~5 min under any write
    # load. Observed in practice: the 3B model misread this repetitive-looking
    # pattern as "high write load" / "repeated restarts", a false positive.
    # Collapse it to a one-line summary when every cycle actually succeeded,
    # so there's nothing repetitive left to misjudge; only pass raw lines
    # through when a cycle didn't complete normally.
    if raw_log.startswith("(") or raw_log == "(no output)":
        return raw_log
    starts = len(re.findall(r"Background saving started", raw_log))
    successes = len(re.findall(r"Background saving terminated with success", raw_log))
    anomalies = [
        line for line in raw_log.splitlines()
        if re.search(r"error|fail|abort|warn", line, re.IGNORECASE)
    ]
    if starts > 0 and starts == successes and not anomalies:
        return f"{starts} routine background-save cycles in the last 30 min, all completed successfully (normal periodic snapshotting, not restarts)."
    if anomalies:
        return "Anomalies found in redis log:\n" + "\n".join(anomalies[-10:])
    return raw_log


def container_floor(ps_text: str) -> str:
    """Deterministic severity floor from container status -- a stopped/crashed
    container is an objective fact, not a judgment call, so don't leave it to
    the 3B model's discretion (it has been observed to notice a container is
    down but still rate it "info"). The LLM still owns the nuanced calls
    (log content, stale task noise)."""
    if ps_text.startswith("("):  # guest exec itself failed
        return "warn"
    lines = [line for line in ps_text.splitlines() if line.strip()]
    if not lines:
        return "warn"
    down = [line for line in lines if "\tUp " not in line]
    if not down:
        return "none"
    return "critical" if len(down) == len(lines) else "warn"


def _container_down_reason(ps_text: str) -> str:
    """Human-readable description of which container(s) are down, for use in
    the notification body when container_floor is what's driving severity --
    the LLM's summary can't be trusted to state this correctly/at all."""
    if ps_text.startswith("("):
        return f"container status check failed: {ps_text}"
    lines = [line for line in ps_text.splitlines() if line.strip()]
    down = [line.replace("\t", ": ") for line in lines if "\tUp " not in line]
    return "; ".join(down)


def fetch():
    ps = host_metrics.guest_exec(VMID, "docker", "ps", "-a", "--format", "{{.Names}}\\t{{.Status}}")
    # / is the VM's own 64G disk (docker images/db); /mnt/photo is the NFS-mounted
    # photo library from the NAS -- that's the one that actually matters for "running
    # out of space for photos".
    disk_root = host_metrics.guest_exec(VMID, "df", "-h", "/")
    disk_photo = host_metrics.guest_exec(VMID, "df", "-h", "/mnt/photo")
    mem = host_metrics.guest_exec(VMID, "free", "-m")

    logs = []
    for name in CONTAINERS:
        out = host_metrics.guest_exec(VMID, "docker", "logs", "--since", "30m", "--tail", "50", name)
        if name == "immich_redis":
            out = _summarize_redis_log(out)
        logs.append(f"--- {name} ---\n{out}")

    text = (
        "=== Immich (VM102): container status ===\n"
        f"{ps}\n\n"
        "=== Immich (VM102): disk usage (VM root / docker) ===\n"
        f"{disk_root}\n\n"
        "=== Immich (VM102): disk usage (photo library, NFS mount) ===\n"
        f"{disk_photo}\n\n"
        "=== Immich (VM102): memory usage ===\n"
        f"{mem}\n\n"
        "=== Immich (VM102): recent container logs (last 30 min) ===\n"
        + "\n\n".join(logs)
    )

    c_floor = container_floor(ps)
    root_floor, root_reason = host_metrics.check_disk_mem("vm102_root", "Immich VM root", disk_root, mem)
    # Photo library only has a disk mount (no separate mem concept), and mem
    # is already covered by root_floor -- pass no mem_text here to skip it.
    # disk_snapshot_floor_pct=90: this NFS mount isn't watched by Prometheus's
    # NodeDiskAlmostFull (mountpoint="/" only), so it needs its own
    # deterministic floor here -- it's large, slow-growing NAS storage, so
    # 90% (matching Prometheus's own convention) rather than flagging every
    # normal fluctuation in the 70s/80s.
    photo_floor, photo_reason = host_metrics.check_disk_mem(
        "vm102_photo", "Photo library", disk_photo, disk_snapshot_floor_pct=90,
    )

    floor = max(c_floor, root_floor, photo_floor, key=SEVERITY_ORDER.index)
    reason = "; ".join(r for r in (
        _container_down_reason(ps) if c_floor != "none" else "",
        root_reason,
        photo_reason,
    ) if r)
    return text, floor, reason


if __name__ == "__main__":
    report_text, floor, reason = fetch()
    print(report_text)
    print(f"\n(combined floor: {floor}; reason: {reason or '(none)'})")
