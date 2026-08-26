#!/usr/bin/env python3
"""Generic disk/RAM health check for a VM reachable only via the QEMU guest
agent (no direct SSH set up) -- used for VM100 (HAOS) and VM101 (desktop).
One parametrized script rather than a near-duplicate per VM.

Usage: ./fetch_vm_log.py <vmid> <label> [disk_path]
       import as fetch_vm_log.fetch(vmid, label, disk_path="/") -> (text, floor, reason)

`disk_path` matters: HAOS (VM100) keeps `/` as a small, deliberately always-
100%-full read-only OS partition by design (immutable A/B layout) -- its
actual writable data lives on `/mnt/data`. Checking `/` there would be a
permanent false "critical". A normal VM (like VM101) just uses "/".
"""
import sys

import host_metrics


def fetch(vmid: str, label: str, disk_path: str = "/"):
    disk = host_metrics.guest_exec(vmid, "df", "-h", disk_path)
    mem = host_metrics.guest_exec(vmid, "free", "-m")

    text = (
        f"=== {label} (VM{vmid}): disk usage ({disk_path}) ===\n"
        f"{disk}\n\n"
        f"=== {label} (VM{vmid}): memory usage ===\n"
        f"{mem}\n"
    )
    floor, reason = host_metrics.check_disk_mem(f"vm{vmid}", label, disk, mem)
    return text, floor, reason


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    path_arg = sys.argv[3] if len(sys.argv) > 3 else "/"
    report_text, result_floor, result_reason = fetch(sys.argv[1], sys.argv[2], path_arg)
    print(report_text)
    print(f"\n(floor: {result_floor}; reason: {result_reason or '(none)'})")
