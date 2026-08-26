# ops-agent — homelab log/health triage (Phase 1 of the local AI project)

A small always-on agent that periodically pulls health signals from the
Proxmox host, VM100 (HAOS), VM101 (desktop), the Immich VM (VM102), and
itself (CT103), has a local LLM triage them, and only pushes a phone
notification when something is actually wrong. Read-only / advisory only —
it never touches any of the monitored machines, it just reads and reports.

## Where it runs

**LXC 103** ("ops-agent") on the Proxmox host `pve`, Debian 13, DHCP IP
(currently `192.168.1.59`), 4 cores / 6 GB RAM / 16 GB disk (bumped from
4 GB after an OOM-kill on Ollama while Docker/Open WebUI + model swapping
were all active at once). Ollama
(`llama3.2:3b`, CPU-only) runs there too, listening on `localhost:11434`.

The scripts run as the `opsagent` user inside the LXC. That user has its
own dedicated ed25519 keypair (`~opsagent/.ssh/id_ed25519`) installed in
the Proxmox host's `root` authorized_keys (restricted:
`no-agent-forwarding,no-X11-forwarding,no-port-forwarding`), separate from
VM101's existing key. An `ssh proxmox` config alias points at the host.

VM102 (Immich) has no direct SSH access set up, so its logs are fetched via
`ssh proxmox` -> `qm guest exec 102 -- ...` rather than a direct SSH hop.

## Open WebUI (general chat)

The same LXC also runs [Open WebUI](https://github.com/open-webui/open-webui)
(Docker, `--network host` so it can reach Ollama at `127.0.0.1:11434` without
any port-mapping/DNS tricks) as a general chat frontend -- unrelated to the
triage pipeline, just reusing the same local models. Browse to
`http://192.168.1.59:8080` from any device on the LAN; first visit creates
the admin account (`WEBUI_AUTH=true`, since it's reachable from the whole
LAN, not just localhost). Chat history persists in the `open-webui` Docker
volume; the container is `--restart always` so it survives LXC reboots.

Local models available: `llama3.2:3b` (best quality/reliability of the
three, current default recommendation), `qwen2.5:0.5b` and `qwen2.5:1.5b`
(pulled 2026-08-25 to compare against Llama -- under host contention all
three land at a similar ~0.5 tok/s, since the bottleneck right now is
host-wide CPU scheduling, not model size; Qwen's real advantage would show
up on a quieter host, but its answer quality/reliability is noticeably
worse than Llama's at the 0.5B size).

Setup is in `setup-openwebui.sh` (installs Docker via the official
convenience script, needs `nesting=1,keyctl=1` LXC features -- already
enabled -- for Docker to work inside an unprivileged container). Same
CPU-only/host-contention caveat as the triage pipeline applies to chat
response latency; see below.

### Cloud model connections (OpenAI/ChatGPT, optionally Anthropic/Claude)

Open WebUI supports adding OpenAI and Anthropic as extra model backends
alongside the local Ollama ones -- confirmed both have native handlers in
this build (`utils/anthropic.py`, `routers/openai.py`). Set via the
`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` env var on the container (added
2026-08-25 for OpenAI). Note this is a different product from a ChatGPT
subscription -- it's OpenAI's pay-as-you-go API (`platform.openai.com`,
separate billing/API key from `chat.openai.com`), so every message costs
real (small) money and isn't private/local like the Ollama models.

**Gotcha**: Open WebUI persists connection config (enabled state, API keys)
to its own SQLite database (`/app/backend/data/webui.db`, `config` table,
JSON-encoded values) on first boot. The `OPENAI_API_KEY` env var only seeds
that config the *first* time the container starts with no existing config
-- adding or changing the key on an already-initialized instance via env
var alone does nothing, because the persisted DB value wins. Fix: update
it directly (`UPDATE config SET value = '["<key>"]' WHERE key =
'openai.api_keys'`, via `docker exec open-webui python3 -c "..."` reading
the key from the container's own env so it's not re-typed anywhere) then
`docker restart open-webui`. Same gotcha will apply if adding the Anthropic
key later, or changing the OpenAI key again.

## Files

| File | Purpose |
|------|---------|
| `host_metrics.py` | shared disk/RAM floor + trend-detection logic, reused by every fetch script below (see "Standardized monitoring") |
| `fetch_proxmox_log.py` | journal warnings, `zpool status -x`, recent failed/warned PVE tasks, last vzdump backup job status (hard floor: any non-`OK` status forces `critical`), host disk usage (RAM deliberately excluded, see below) |
| `fetch_immich_log.py` | Immich container status/logs + disk usage (VM root disk *and* the NFS-mounted `/mnt/photo` library — those are separate filesystems, see note below) + RAM. Hard floors: any non-`Up` container forces `warn`/`critical`; either disk ≥90% used forces `warn`, ≥95% forces `critical`. Redis's routine background-save log lines are collapsed to a one-line summary (see below) rather than passed raw. |
| `fetch_vm_log.py` | generic disk+RAM check for a VM reachable only via the QEMU guest agent — used for VM100 (HAOS) and VM101 (desktop), one parametrized script rather than near-duplicates |
| `fetch_self_log.py` | self-check for this LXC: disk, RAM, and whether Ollama/Docker/the timer itself are actually alive |
| `triage.py` | sends the fetched report to Ollama, parses a `SEVERITY: .. / SUMMARY: ..` verdict |
| `verify_with_cloud.py` | second opinion from OpenAI `gpt-4o-mini` for narrative (non-floor-driven) candidate alerts, see below |
| `notify.py` | POSTs to the shared HA webhook — only called when severity is `warn` or `critical` |
| `run_all.py` | fetch -> triage -> (cloud-verify if narrative) -> notify, the systemd timer's entry point |
| `ops-triage.service` / `ops-triage.timer` | oneshot + timer, runs every 20 min |
| `deploy.sh` | pushes everything into LXC 103 and (re)installs the systemd units |
| `setup-openwebui.sh` | one-time Docker + Open WebUI setup, see below |
| `webhook_url` | **gitignored** — contains the HA webhook URL, see below |
| `openai_api_key` | **gitignored** — same key added to Open WebUI, used by `verify_with_cloud.py` |

Note on Immich disk usage: VM102's own disk is only 64 GB, but the actual
photo library lives on the NAS and is NFS-mounted at `/mnt/photo` (11 TB,
~73% used at the time this was set up) — it is *not* stored on VM102's
local disk (24% used). An earlier assumption that the 64 GB VM disk was
close to the library size was wrong; both mounts are checked separately so
triage isn't misled by either one.

**False positives caught in practice (2026-08-25)** -- both fixed the same
way, moving an objectively-checkable fact out of the model's judgment and
into code rather than trusting it to read numbers/patterns correctly every
time: (1) the model flagged Redis's completely normal periodic
background-save cycles (new PID every ~5 min, by design) as "high write
load" / "repeated restarts" on a container that had been healthy for 2
weeks straight -- fixed by collapsing that log to a one-line summary when
every cycle actually succeeded. (2) the model flagged `/mnt/photo` at 73%
used as "nears full disk", despite the prompt's own stated ~90% threshold
-- fixed by computing the disk-usage floor in code instead of trusting the
model's reading of the percentage.

**A day-1 monitoring pass (2026-08-26) surfaced a deeper problem**: the
disk-usage floor fix above only sets a *minimum* -- `severity =
max(llm_severity, floor)` -- so the model could still independently claim
`warn` on its own and win, which it did again on the same 73%-used
`/mnt/photo`. Worse, on a separate cycle it fabricated "immich_server
encountered errors with asset thumbnail generation" when that container's
log fetch had come back **completely empty** -- not a misread, an outright
invention with zero basis in the data it was given.

Fixed with a second layer in `run_all.py`: `ANOMALY_PATTERN`, a regex of
failure-indicating keywords (error, exception, fail, crash, degraded,
unhealthy, etc.). If the LLM's severity is *above* the deterministic floor
and none of those keywords appear anywhere in the raw report text, its
claim is capped back down to the floor -- the model is no longer trusted to
escalate severity on its own say-so without something in the actual data to
point to. Confirmed working: a later cycle tried "immich_redis disk usage
over 90%" (not even a real metric) and got correctly capped to `none`.

**Gotcha hit while building the gate itself**: the report's own "nothing
wrong" text has to avoid the same keywords it's being scanned for, or the
gate is a no-op. The section header `"=== Proxmox host: failed/warned tasks
..."` and the placeholder `"(no failed/warned tasks in the last 30 min)"`
both unconditionally contained "failed" -- meaning *every* report ever
generated had "corroborating evidence" by definition, regardless of actual
content, and the gate did nothing on its first deploy. Renamed both to
avoid trigger words (`"task issues"` / `"(none in the last 30 min)"`). Any
new "everything's fine" message added to a fetch script needs the same
check against `ANOMALY_PATTERN` in `run_all.py`.

## Standardized monitoring across every machine (added 2026-08-26)

Originally this only checked the Proxmox host and Immich/VM102. Extended to
cover VM100 (HAOS), VM101 (desktop), and this LXC's own health, using the
same disk/RAM floor logic everywhere via the shared `host_metrics.py` rather
than duplicating thresholds per target. VM100/VM101 have no direct SSH set
up either, so they're reached the same way as Immich: `ssh proxmox` -> `qm
guest exec <vmid>`. CT103's self-check runs local commands with no SSH hop
at all, since it's already the box the code runs on.

**Trend detection**, not just a snapshot threshold: each cycle appends a
disk%/RAM% sample per target to `metrics_history.json` (gitignored,
runtime-only, ~8h rolling window). If a resource has climbed 15+ percentage
points over ~2h and is already at/above 60%, that's flagged as its own
`warn` -- specifically to catch a slow leak *before* it crosses the hard
90%/95% threshold, which a plain snapshot never would.

**Two false positives caught rolling this out, both fixed before it shipped**:
1. HAOS (VM100) keeps `/` as a small, deliberately always-100%-full
   read-only OS partition by design (immutable A/B layout) -- checking it
   would be a permanent false "critical". Real writable data lives on
   `/mnt/data` (`fetch_vm_log.fetch()` takes a `disk_path` argument now,
   defaulting to `/` for a normal VM like VM101).
2. The Proxmox host's own RAM legitimately runs ~90%+ "used" as its normal
   baseline (static VM/LXC reservations + ZFS ARC using available RAM by
   design) -- unlike a guest VM, where that really would mean trouble. It
   fired a "warn" on the very first run despite nothing being wrong.
   Deliberately **not** checked for the host specifically (disk still is);
   every other target keeps the normal 90%/95% floor.

**Known limitation, not yet fixed**: the repeat-notification throttle keys
on the floor combination (`severity|host|immich|vm100|vm101|self`). That's
precise for floor-driven alerts, but for a purely narrative one (all floors
`none`, only the LLM's own reading crossed the anomaly gate + cloud check)
two *different* real issues occurring back to back would share the same
key and the second could get incorrectly suppressed as "already notified".
Not fixed because there's no cheap way to fingerprint "is this the same
issue" from paraphrased LLM text without another model call; worth
revisiting only if it actually causes a real miss in practice.

## Cloud second-opinion for narrative alerts (added 2026-08-26)

Floor-driven alerts (container down, backup failed, disk genuinely over
threshold) are already fully deterministic and don't need a second opinion.
But the local 3B model's own *narrative* judgment calls -- the ones that
passed the anomaly-evidence gate above because real evidence exists
somewhere in the report, but where the model's read of how serious it is
has repeatedly been unreliable -- get one more check before paging the
phone: `verify_with_cloud.py` sends the same raw report + the local model's
claim to OpenAI `gpt-4o-mini`, and the alert only survives if the cloud
model agrees it's real. If it disagrees, the notification is suppressed
entirely (deliberate choice: trust the more capable model over the small
local one). If the cloud call itself fails (network, quota, etc.), `run_all.py`
logs it and falls back to the local read rather than silently blocking a
possibly-real alert.

Verified against two synthetic cases before relying on it: a fabricated
claim over genuinely empty logs → correctly denied; a real `ERROR` message
paired with a matching claim → correctly confirmed.

Cost is negligible -- this only fires on candidate alerts (rare), never on
the routine 20-min cycle, and `gpt-4o-mini` is priced in fractions of a
cent per call.

## Backup job monitoring

PVE's native daily vzdump backup notification used to page every day, success
or not (PVE's own `default-matcher` routed *all* severities to the webhook).
Fixed by excluding `type=vzdump` from that matcher (`pvesh set
/cluster/notifications/matchers/default-matcher --match-field
'exact:type=vzdump' --invert-match 1`) — vzdump notifications now go nowhere
natively. Instead, `fetch_proxmox_log.py` checks the last vzdump task's
status itself each cycle (`pvesh get /nodes/pve/tasks --typefilter vzdump
--limit 1`) and applies a hard floor (`critical` if status != `OK`), so a
failed backup gets caught by the same triage pipeline as everything else
instead of a separate always-on native alert. Trade-off: up to ~20 min
delay vs. the instant native alert PVE would otherwise send on failure.

## Notifications

Reuses the existing PVE `ha-webhook` notification target
(`http://192.168.1.5:8123/api/webhook/proxmox_alert_wylku5g6`) that already
feeds `automation.proxmox_alert` -> `notify.mobile_app_iphone_peterl`. This
was a deliberate choice: ops-agent alerts land in the same "proxmox" phone
notification group as PVE's own native alerts, rather than a separate
channel. The HA automation itself does no severity filtering — it pushes
whatever hits the webhook — so **all** the throttling/quiet-on-nothing-
notable logic lives in `triage.py` + `run_all.py`'s `ALERT_LEVELS` check,
not in HA.

`webhook_url` is gitignored (same convention as `homeassistant/token`) —
recreate it locally if you're re-cloning:
```
echo "http://192.168.1.5:8123/api/webhook/proxmox_alert_wylku5g6" > webhook_url
```

### Notification content + throttling (fixed 2026-08-26, after an alert storm)

A day-1 monitoring pass turned up a real backup failure (see "Backup job
monitoring" above) that also exposed two design bugs, both now fixed:

1. **Notification content was wrong when a hard floor was driving
   severity.** The message body always came from the LLM's own summary,
   even when a floor (container/backup) was the actual reason for the
   elevated severity -- so every notification described something
   unrelated (or fabricated) instead of the real problem. Fixed: when a
   hard floor is at or above the LLM's own severity, the notification body
   is now built from the floor's own concrete reason text (e.g. `"Last
   vzdump backup job (...): status = job errors"`), not the LLM's
   narrative. `fetch_proxmox_log.fetch()` and `fetch_immich_log.fetch()`
   both now return `(text, floor, reason)` instead of `(text, floor)`.
2. **No throttling at all.** An unresolved condition re-notified on every
   single 20-min cycle -- one real backup failure produced ~18 near-
   identical "CRITICAL" phone pages overnight. Fixed with `state.json`
   (gitignored, runtime-only) tracking a `severity|container_floor|
   backup_floor` condition key: the same condition only re-notifies once
   per `RENOTIFY_INTERVAL_S` (4h), and the state clears itself the moment
   the condition resolves so a *future* recurrence isn't stuck throttled
   by an old timestamp.

## Deploy

```bash
./deploy.sh
```

Stages every file through the Proxmox host (`pct push`, since there's no
direct filesystem route into the LXC from the workstation), installs the
systemd units, and enables the timer.

## Manual runs / debugging

```bash
# one fetch script at a time
ssh proxmox "pct exec 103 -- su - opsagent -c 'cd ops-agent && ./fetch_proxmox_log.py'"
ssh proxmox "pct exec 103 -- su - opsagent -c 'cd ops-agent && ./fetch_immich_log.py'"

# full pipeline, once
ssh proxmox "pct exec 103 -- su - opsagent -c 'cd ops-agent && ./run_all.py'"

# timer status / logs
ssh proxmox "pct exec 103 -- systemctl status ops-triage.timer"
ssh proxmox "pct exec 103 -- journalctl -u ops-triage --no-pager -n 50"

# force a test notification
ssh proxmox "pct exec 103 -- su - opsagent -c 'cd ops-agent && ./notify.py \"Test\" \"ops-agent test notification\" warn'"
```

## Known constraint: CPU-only inference is slow under host contention

The Proxmox host has 12 threads total, already allocated 2+4+2 across
VM100/101/102 before this LXC existed. A cold/contended `llama3.2:3b` run
was measured at ~2.5 minutes for a short triage response when the host
load average was ~6.3 (VM101's desktop actively in use). The LXC was
bumped to 4 cores to help, but a triage cycle can still take a few
minutes — that's fine given the 20-minute timer interval, but don't expect
sub-second responses if this is ever turned into something interactive
(e.g. the "ask HA history" chat idea from the roadmap — that would want a
lighter model or its own less-contended host).

## Out of scope (this phase)

No auto-remediation, no Home Assistant log integration (production HA is
the NAS docker container, not a current target), no VM101 log integration,
no camera/vision or presence work — see the local-AI roadmap memory for
those.
