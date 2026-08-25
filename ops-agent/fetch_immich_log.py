#!/usr/bin/env python3
"""Pull container health, recent logs, and disk usage for Immich (VM102).

Goes LXC -> ssh proxmox -> `qm guest exec 102`, since VM102 has no direct
SSH access set up.

Usage: ./fetch_immich_log.py
"""
import json
import re
import shlex
import subprocess

SSH_HOST = "proxmox"
VMID = "102"
CONTAINERS = ["immich_server", "immich_postgres", "immich_machine_learning", "immich_redis"]
SEVERITY_ORDER = ["none", "info", "warn", "critical"]


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


def _guest_exec(*args):
    # ssh with multiple trailing argv elements naively space-joins them into one
    # remote command string with no quoting -- build (and quote) that string
    # ourselves instead, or arguments containing spaces/backslashes/braces get
    # mangled by the remote shell before qm/docker ever see them.
    remote_cmd = f"qm guest exec {VMID} -- " + " ".join(shlex.quote(a) for a in args)
    result = subprocess.run(["ssh", SSH_HOST, remote_cmd], capture_output=True, text=True, timeout=30)
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return f"(guest exec failed: {result.stdout or result.stderr})"
    if parsed.get("err-data"):
        return f"(guest exec error, exitcode={parsed.get('exitcode')}: {parsed['err-data'].strip()})"
    return parsed.get("out-data", "").strip() or "(no output)"


def container_floor(ps_text: str) -> str:
    """Deterministic severity floor from container status -- a stopped/crashed
    container is an objective fact, not a judgment call, so don't leave it to
    the 3B model's discretion (it has been observed to notice a container is
    down but still rate it "info"). The LLM still owns the nuanced calls
    (log content, disk trends, stale task noise)."""
    if ps_text.startswith("("):  # guest exec itself failed
        return "warn"
    lines = [line for line in ps_text.splitlines() if line.strip()]
    if not lines:
        return "warn"
    down = [line for line in lines if "\tUp " not in line]
    if not down:
        return "none"
    return "critical" if len(down) == len(lines) else "warn"


def disk_usage_floor(df_text: str) -> str:
    """Deterministic severity floor from a `df -h` Use% column -- another
    objective, checkable number the 3B model has been observed to misjudge
    (flagged 73% used as "nears full disk" despite the prompt's own stated
    ~90% threshold). Matches that same threshold, just computed in code
    instead of trusted to the model's arithmetic/reading."""
    lines = [line for line in df_text.splitlines() if line.strip()]
    if len(lines) < 2:
        return "none"
    fields = lines[1].split()
    pct_field = next((f for f in fields if f.endswith("%")), None)
    if not pct_field:
        return "none"
    try:
        pct = int(pct_field.rstrip("%"))
    except ValueError:
        return "none"
    if pct >= 95:
        return "critical"
    if pct >= 90:
        return "warn"
    return "none"


def fetch():
    # A literal backslash-t (not an actual tab byte) is what docker's --format
    # itself expands to a tab; shlex.quote above keeps it intact end to end.
    ps = _guest_exec("docker", "ps", "-a", "--format", "{{.Names}}\\t{{.Status}}")
    # / is the VM's own 64G disk (docker images/db); /mnt/photo is the NFS-mounted
    # photo library from the NAS -- that's the one that actually matters for "running
    # out of space for photos".
    disk_root = _guest_exec("df", "-h", "/")
    disk_photo = _guest_exec("df", "-h", "/mnt/photo")

    logs = []
    for name in CONTAINERS:
        out = _guest_exec("docker", "logs", "--since", "30m", "--tail", "50", name)
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
        "=== Immich (VM102): recent container logs (last 30 min) ===\n"
        + "\n\n".join(logs)
    )
    floor = max(
        container_floor(ps),
        disk_usage_floor(disk_root),
        disk_usage_floor(disk_photo),
        key=SEVERITY_ORDER.index,
    )
    return text, floor


if __name__ == "__main__":
    report_text, floor = fetch()
    print(report_text)
    print(f"\n(combined container+disk floor: {floor})")
