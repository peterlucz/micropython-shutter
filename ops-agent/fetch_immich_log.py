#!/usr/bin/env python3
"""Pull container health, recent logs, disk usage, and RAM for Immich (VM102).

Goes LXC -> ssh proxmox -> `qm guest exec 102`, since VM102 has no direct
SSH access set up.

Usage: ./fetch_immich_log.py
"""
import json
import re
import urllib.request

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


def _summarize_if_clean(raw_log: str, name: str) -> str:
    """Collapse a container's log window to a deterministic one-liner when it
    contains no anomaly-indicating line at all. Unlike the redis/postgres
    summarizers above, this isn't reinterpreting one specific known-benign
    error -- it's just removing INFO-level noise so there's nothing left for
    the LLM to invent an unrelated claim *about*.

    Found 2026-09-11: the local 3B model fabricated "incomplete JPEG scans
    and premature end of JPEG images" for immich_server's log window when
    the real 30-min fetch had zero matching lines (confirmed after the fact:
    zero 'jpeg'/'premature'/'corrupt' hits across a full 48h of actual
    immich_server logs, container uptime 7 days, no restart). Worse, the
    cloud-verification step (verify_with_cloud.py) also "confirmed" it --
    its prompt asks the cloud model to find supporting text in the raw
    report, but a container's log tail can be long enough, and phrased
    plausibly-Immich-adjacent enough, that a cheap model still pattern-
    matches its way to agreement rather than actually checking. This isn't
    a specific-error case like the postgres/redis ones above (there's no
    single known pattern to name), so the fix is generic: if literally
    nothing anomaly-shaped is in the window, say so in one deterministic
    sentence instead of handing the model 50 lines of ordinary INFO output
    to draw its own conclusions from."""
    if raw_log.startswith("(") or raw_log == "(no output)":
        return raw_log
    anomalies = [
        line for line in raw_log.splitlines()
        if re.search(
            r"error|fail(?:ed|ing)?|exception|warn|corrupt(?:ed)?|premature|panic|denied|refused|"
            r"timed? ?out",
            line, re.IGNORECASE,
        )
    ]
    if anomalies:
        return f"Anomalies found in {name} log:\n" + "\n".join(anomalies[-10:])
    n = len(raw_log.splitlines())
    return f"{n} routine log line(s) for {name} in the last 30 min, no errors/warnings found."


def _summarize_postgres_log(raw_log: str) -> tuple:
    # UQ_assets_owner_checksum fires only when a client tries to (re-)insert
    # an asset whose content checksum already exists for that owner -- by
    # definition this can only mean "already backed up", never data loss or
    # corruption; Immich's own DB is doing exactly its job rejecting it.
    # Found 2026-09-07: a mobile client with a stuck upload-queue entry can
    # retry the same handful of already-uploaded photos indefinitely (every
    # ~10-15 min), and each retry looks like a fresh error to the log, so the
    # 3B model re-flagged it as "warn" on every single triage cycle even
    # though it's the identical, already-known, harmless situation each
    # time. Same fix shape as the Redis background-save case above: collapse
    # to a one-line summary rather than passing raw repeated ERROR blocks
    # through, so there's nothing that reads as "new" for the model to
    # misjudge. Other Postgres errors (an actual constraint we don't
    # recognize, connection issues, etc.) still pass through raw.
    #
    # Second `is_known_benign` return value added 2026-09-11: the original
    # design relied on the LLM's own final summary text still describing
    # this recognizably enough for a downstream regex to catch and re-cap
    # (see run_all.py's old KNOWN_BENIGN_NARRATIVES) -- in practice the local
    # model's wording varies enough, and the cloud-verification step's
    # wording varies more, that the regex missed real instances (confirmed
    # via alert_log.jsonl: several "warn" pages went out, cloud_verdict=
    # "confirmed", for this exact already-explained condition with
    # immich_floor=="none" throughout). A plain boolean from the one place
    # that actually knows the ground truth is robust to any amount of
    # downstream rewording; run_all.py uses it to cap severity *before*
    # ever reaching the cloud-verification call, rather than hoping to catch
    # it after the fact.
    if raw_log.startswith("(") or raw_log == "(no output)":
        return raw_log, False
    dup_checksum = re.findall(
        r'duplicate key value violates unique constraint "UQ_assets_owner_checksum"', raw_log,
    )
    other_errors = [
        line for line in raw_log.splitlines()
        if re.search(r"error|fatal|panic", line, re.IGNORECASE)
        and "UQ_assets_owner_checksum" not in line
    ]
    if dup_checksum and not other_errors:
        return (
            f"{len(dup_checksum)} duplicate-checksum rejection(s) on asset inserts in the last 30 min "
            "-- a client re-uploading content it has already backed up successfully; Immich's DB "
            "correctly rejects these, no data loss or corruption. Known benign pattern, not actionable.",
            True,
        )
    if other_errors:
        return "Anomalies found in postgres log:\n" + "\n".join(other_errors[-10:]), False
    return raw_log, False


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


def _get_ml_urls() -> list:
    """Read Immich's own configured machine-learning backend URL(s) straight
    from its database (Settings -> Machine Learning -> URLs in the admin UI)
    -- the actual source of truth for which backend(s) Immich will try, and
    not visible in any docker-compose/env file on either VM. This exact gap
    let a stale entry (VM101's pre-migration IP) sit unnoticed for hours
    after VM101's VLAN move, 2026-09-04, caught only by hand -- see
    local-ai-gpu-hardware-options.md."""
    out = host_metrics.guest_exec(
        VMID, "docker", "exec", "immich_postgres", "psql", "-U", "postgres", "-d", "immich",
        "-tA", "-c",
        "SELECT value->'machineLearning'->'urls' FROM system_metadata WHERE key = 'system-config';",
    )
    try:
        return json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return []


def remote_ml_floor(urls: list) -> tuple:
    """Immich's *local* ML container is already covered by container_floor
    above (it's in CONTAINERS, checked via qm guest exec). Any *other* URL in
    this list is a remote backend (currently: VM101's GPU-accelerated
    OpenVINO container, see local-ai-gpu-hardware-options.md) reachable only
    over the network -- check it directly with a real HTTP request rather
    than trusting Immich's own healthy/unhealthy log lines, since a stale or
    unreachable URL just quietly stops appearing in those logs rather than
    producing an obvious error to grep for."""
    remote = [u for u in urls if "immich-machine-learning" not in u]
    if not remote:
        return "none", ""
    problems = []
    for url in remote:
        try:
            with urllib.request.urlopen(f"{url}/ping", timeout=6) as resp:
                if resp.status != 200:
                    problems.append(f"{url} returned HTTP {resp.status}")
        except Exception as e:
            problems.append(f"{url} unreachable: {e}")
    if problems:
        # warn, not critical -- Immich still works via the local CPU fallback,
        # this is a silent performance regression, not an outage.
        return "warn", "Remote ML backend unreachable, silently degraded to CPU-only inference: " + "; ".join(problems)
    return "none", ""


def fetch():
    ps = host_metrics.guest_exec(VMID, "docker", "ps", "-a", "--format", "{{.Names}}\\t{{.Status}}")
    # / is the VM's own 64G disk (docker images/db); /mnt/photo is the NFS-mounted
    # photo library from the NAS -- that's the one that actually matters for "running
    # out of space for photos".
    disk_root = host_metrics.guest_exec(VMID, "df", "-h", "/")
    disk_photo = host_metrics.guest_exec(VMID, "df", "-h", "/mnt/photo")
    mem = host_metrics.guest_exec(VMID, "free", "-m")

    logs = []
    known_benign = False
    for name in CONTAINERS:
        out = host_metrics.guest_exec(VMID, "docker", "logs", "--since", "30m", "--tail", "50", name)
        if name == "immich_redis":
            out = _summarize_redis_log(out)
        elif name == "immich_postgres":
            out, benign = _summarize_postgres_log(out)
            known_benign = known_benign or benign
        elif name in ("immich_server", "immich_machine_learning"):
            out = _summarize_if_clean(out, name)
        logs.append(f"--- {name} ---\n{out}")

    ml_urls = _get_ml_urls()
    ml_floor, ml_reason = remote_ml_floor(ml_urls)

    text = (
        "=== Immich (VM102): container status ===\n"
        f"{ps}\n\n"
        "=== Immich (VM102): configured machine-learning backend(s) ===\n"
        f"{ml_urls or '(could not read system-config)'}"
        + (f"  <-- PROBLEM: {ml_reason}" if ml_floor != "none" else "  (all reachable)")
        + "\n\n"
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

    floor = max(c_floor, root_floor, photo_floor, ml_floor, key=SEVERITY_ORDER.index)
    reason = "; ".join(r for r in (
        _container_down_reason(ps) if c_floor != "none" else "",
        root_reason,
        photo_reason,
        ml_reason,
    ) if r)
    return text, floor, reason, known_benign


if __name__ == "__main__":
    report_text, floor, reason, benign = fetch()
    print(report_text)
    print(f"\n(combined floor: {floor}; reason: {reason or '(none)'}; known_benign: {benign})")
