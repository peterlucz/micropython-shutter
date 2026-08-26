#!/usr/bin/env python3
"""Self-check for CT103 (this LXC) -- disk, RAM, and whether ops-agent's own
critical services are actually alive. Nothing else was watching this box;
if it degrades (or Ollama/Docker/the timer itself dies), the whole pipeline
could go quiet without anyone noticing. Runs local commands only, no SSH hop
needed since this already runs on the box being checked.

Usage: ./fetch_self_log.py
       import as fetch_self_log.fetch() -> (text, floor, reason)
"""
import host_metrics

SERVICES = ["ollama", "docker", "ops-triage.timer"]
SEVERITY_ORDER = host_metrics.SEVERITY_ORDER


def _service_status():
    down = []
    for svc in SERVICES:
        state = host_metrics.run_local("systemctl", "is-active", svc)
        if state != "active":
            down.append(f"{svc}: {state}")
    return down


def fetch():
    disk = host_metrics.run_local("df", "-h", "/")
    mem = host_metrics.run_local("free", "-m")
    down_services = _service_status()

    text = (
        "=== ops-agent (CT103, self): disk usage ===\n"
        f"{disk}\n\n"
        "=== ops-agent (CT103, self): memory usage ===\n"
        f"{mem}\n\n"
        "=== ops-agent (CT103, self): critical services ===\n"
        + ("; ".join(down_services) if down_services else "all active (ollama, docker, ops-triage.timer)")
        + "\n"
    )

    disk_mem_floor, disk_mem_reason = host_metrics.check_disk_mem("self", "ops-agent LXC", disk, mem)
    # A dead critical service on the box that's supposed to be watching
    # everything else is at least as important as a disk/RAM threshold --
    # objective fact, not a judgment call, so it's a hard floor like the rest.
    service_floor = "critical" if down_services else "none"
    service_reason = f"ops-agent LXC service(s) down: {'; '.join(down_services)}" if down_services else ""

    floor = max(disk_mem_floor, service_floor, key=SEVERITY_ORDER.index)
    reason = "; ".join(r for r in (disk_mem_reason, service_reason) if r)
    return text, floor, reason


if __name__ == "__main__":
    report_text, result_floor, result_reason = fetch()
    print(report_text)
    print(f"\n(floor: {result_floor}; reason: {result_reason or '(none)'})")
