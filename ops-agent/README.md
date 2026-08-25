# ops-agent — Proxmox / Immich log triage (Phase 1 of the local AI project)

A small always-on agent that periodically pulls health signals from the
Proxmox host and the Immich VM (VM102), has a local LLM triage them, and
only pushes a phone notification when something is actually wrong.
Read-only / advisory only — it never touches the host or VM102, it just
reads logs and reports.

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
| `fetch_proxmox_log.py` | journal warnings, `zpool status -x`, recent failed/warned PVE tasks, last vzdump backup job status (hard floor: any non-`OK` status forces `critical`) |
| `fetch_immich_log.py` | Immich container status/logs + disk usage (VM root disk *and* the NFS-mounted `/mnt/photo` library — those are separate filesystems, see note below). Hard floors: any non-`Up` container forces `warn`/`critical`; either disk ≥90% used forces `warn`, ≥95% forces `critical`. Redis's routine background-save log lines are collapsed to a one-line summary (see below) rather than passed raw. |
| `triage.py` | sends the fetched report to Ollama, parses a `SEVERITY: .. / SUMMARY: ..` verdict |
| `notify.py` | POSTs to the shared HA webhook — only called when severity is `warn` or `critical` |
| `run_all.py` | fetch -> triage -> notify, the systemd timer's entry point |
| `ops-triage.service` / `ops-triage.timer` | oneshot + timer, runs every 20 min |
| `deploy.sh` | pushes everything into LXC 103 and (re)installs the systemd units |
| `setup-openwebui.sh` | one-time Docker + Open WebUI setup, see below |
| `webhook_url` | **gitignored** — contains the HA webhook URL, see below |

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
