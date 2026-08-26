#!/usr/bin/env python3
"""Shared, target-agnostic health checks reused across every monitored
target (Proxmox host, VM100/101/102, and this LXC's own self-check) --
extracted here instead of duplicated per fetch script, since the same
disk/RAM thresholds and trend logic apply everywhere it makes sense.
"""
import json
import pathlib
import shlex
import subprocess
import time

SSH_HOST = "proxmox"
SEVERITY_ORDER = ["none", "info", "warn", "critical"]

HISTORY_FILE = pathlib.Path(__file__).parent / "metrics_history.json"
HISTORY_WINDOW_S = 8 * 60 * 60   # keep ~8h of samples per target/metric
TREND_LOOKBACK_S = 2 * 60 * 60   # compare current reading against ~2h ago
TREND_RISE_PCT = 15              # flag a rise of this many points over that window
TREND_FLOOR_PCT = 60             # ...but only once usage is already at a meaningful level


def guest_exec(vmid, *args):
    """Run a command inside a VM via the QEMU guest agent (ssh proxmox -> qm
    guest exec). Builds one pre-quoted remote command string -- ssh naively
    space-joins multiple trailing argv elements with no quoting, which
    mangles anything with spaces/braces/tabs before qm/the guest ever see it."""
    remote_cmd = f"qm guest exec {vmid} -- " + " ".join(shlex.quote(a) for a in args)
    result = subprocess.run(["ssh", SSH_HOST, remote_cmd], capture_output=True, text=True, timeout=30)
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return f"(guest exec failed: {result.stdout or result.stderr})"
    if parsed.get("err-data"):
        return f"(guest exec error, exitcode={parsed.get('exitcode')}: {parsed['err-data'].strip()})"
    return parsed.get("out-data", "").strip() or "(no output)"


def run_local(*args):
    """Run a command directly on this box -- used for CT103's own
    self-check, where no SSH hop is needed at all."""
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    return result.stdout.strip() or "(no output)"


def disk_usage_pct(df_text: str):
    lines = [line for line in df_text.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    fields = lines[1].split()
    pct_field = next((f for f in fields if f.endswith("%")), None)
    if not pct_field:
        return None
    try:
        return int(pct_field.rstrip("%"))
    except ValueError:
        return None


def mem_usage_pct(free_text: str):
    """Parse `free -m`. % used is based on the 'available' column (accounts
    for reclaimable cache) rather than naive used/total, matching how
    htop/most tools report real memory pressure."""
    for line in free_text.splitlines():
        if line.startswith("Mem:"):
            fields = line.split()
            if len(fields) < 7:
                return None
            try:
                total, available = int(fields[1]), int(fields[6])
            except ValueError:
                return None
            return round(100 * (total - available) / total) if total else None
    return None


def _pct_floor(pct):
    if pct is None:
        return "none"
    if pct >= 95:
        return "critical"
    if pct >= 90:
        return "warn"
    return "none"


def _load_history():
    if not HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(HISTORY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _record_and_check_trend(target: str, metric: str, pct):
    """Append this sample, prune anything older than HISTORY_WINDOW_S, and
    flag a sustained rise as its own floor+reason -- catches a resource
    creeping toward trouble well before the hard disk/RAM threshold would
    fire on its own (e.g. a leak climbing 40% -> 85% over a few hours would
    sit under a 90% floor the whole time otherwise)."""
    if pct is None:
        return "none", ""
    now = time.time()
    history = _load_history()
    key = f"{target}:{metric}"
    samples = [s for s in history.get(key, []) if now - s[0] <= HISTORY_WINDOW_S]
    samples.append([now, pct])
    history[key] = samples
    HISTORY_FILE.write_text(json.dumps(history))

    old_enough = [s for s in samples if now - s[0] >= TREND_LOOKBACK_S]
    if not old_enough:
        return "none", ""  # not enough history yet -- correct to stay quiet
    baseline_ts, baseline_pct = old_enough[-1]  # closest sample to ~2h ago
    rise = pct - baseline_pct
    if rise >= TREND_RISE_PCT and pct >= TREND_FLOOR_PCT:
        hours = (now - baseline_ts) / 3600
        return "warn", f"{target} {metric} usage climbing: {baseline_pct}% -> {pct}% over ~{hours:.1f}h"
    return "none", ""


def check_disk_mem(target: str, label: str, disk_text: str, mem_text: str = ""):
    """Combine disk + RAM floors and trend detection for one target into a
    single (floor, reason) pair, in the same shape every fetch script uses.
    `target` is a short stable key (e.g. "vm101") for the trend history;
    `label` is what shows up in human-readable notification text."""
    d_pct = disk_usage_pct(disk_text)
    m_pct = mem_usage_pct(mem_text) if mem_text else None
    d_floor = _pct_floor(d_pct)
    m_floor = _pct_floor(m_pct)
    d_trend_floor, d_trend_reason = _record_and_check_trend(target, "disk", d_pct)
    m_trend_floor, m_trend_reason = _record_and_check_trend(target, "mem", m_pct)

    floor = max(d_floor, m_floor, d_trend_floor, m_trend_floor, key=SEVERITY_ORDER.index)
    reasons = []
    if d_floor != "none":
        reasons.append(f"{label} disk at {d_pct}%")
    if m_floor != "none":
        reasons.append(f"{label} memory at {m_pct}%")
    if d_trend_reason:
        reasons.append(d_trend_reason)
    if m_trend_reason:
        reasons.append(m_trend_reason)
    return floor, "; ".join(reasons)
