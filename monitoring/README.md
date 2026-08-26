# Prometheus + Grafana monitoring stack

Standard metrics-based monitoring, complementing (not replacing) the
LLM-based `ops-agent/` log-triage pipeline. See that project's README for
the reasoning behind running both: Prometheus/Grafana handle the
deterministic, numeric side (disk %, RAM %, container up/down, trend
graphs) far better than hand-rolled floor checks; the LLM pipeline stays
scoped to reading unstructured logs and catching things no metric captures.

## Where it runs

New LXC, CT ID 104, hostname `monitoring`, IP `192.168.1.12`. 2 cores / 2GB
RAM (host only had ~3.8GB genuinely unreserved at the time -- bump if it
gets tight, same as CT103's RAM history). Docker + Docker Compose, nesting
enabled at creation (`pct create ... --features nesting=1`).

## Stack (docker-compose.yml)

- **Prometheus** (`:9090`) -- scrapes every target below every 30s, 30-day
  retention.
- **Grafana** (`:3000`) -- dashboards. `GF_SECURITY_ADMIN_PASSWORD` in
  docker-compose.yml only seeds the `admin` account on first boot; Grafana
  then stores whatever password is actually set in its own database
  (`grafana_data` volume), so changing it in the UI persists across
  restarts/redeploys. Password has been changed from the initial value --
  the env var in the committed file is now stale/first-boot-only, not the
  current password.
- **Alertmanager** (`:9093`) -- wired into Prometheus but currently routes
  everything to a `null` receiver (no-op). Alerting rules exist
  (`alert_rules.yml`) and fire in Prometheus/Alertmanager's UI, but nothing
  pages a phone yet -- that's a deliberate scope cut for this first pass,
  not an oversight. Natural next step: point the `null` receiver at the
  same HA webhook `ops-agent/notify.py` already uses, so both pipelines
  converge on one phone-notification path.
- **pve-exporter** (`:9221`) -- queries the Proxmox API *remotely* (never
  installed on the bare host, matching this project's "keep the host lean"
  convention) using a dedicated read-only API token.

## Targets scraped

| Target | Method | Notes |
|---|---|---|
| Proxmox host | `pve-exporter` → PVE API | Read-only `monitoring@pve` user, `PVEAuditor` role, API token `prometheus`. Token lives in gitignored `pve_token`, templated into `pve.yml` at deploy time from `pve.yml.template`. |
| VM101 (desktop) | `node_exporter` (apt, `prometheus-node-exporter`), installed directly -- this is the machine these sessions run on | `192.168.1.26:9100` |
| VM102 (Immich) | `node_exporter` (apt) + `cAdvisor` (docker container, `gcr.io/cadvisor/cadvisor`) | `192.168.1.99:9100` and `:8080` -- cAdvisor gives per-container CPU/mem/network for the Immich stack |
| CT103 (ops-agent) | `node_exporter` (apt) | `192.168.1.59:9100` |
| CT104 (monitoring, self) | `node_exporter` (apt) | `192.168.1.12:9100` -- self-monitoring; note the scrape target must be the LXC's real IP, not `localhost` (that resolves to the Prometheus *container* itself under docker-compose's bridge network, not the LXC host) |
| VM100 (HAOS) | **not monitored at the host/OS level** | HAOS is a locked-down appliance (immutable, no generic package manager, no SSH, only reachable via `qm guest exec`) -- there's no supported way to run `node_exporter` on it. Home Assistant does have its own built-in Prometheus integration (`/api/prometheus`) for *entity* metrics, which is a different, optional scope not set up here. |

## Dashboards (imported via Grafana's HTTP API from grafana.com)

- **Node Exporter Full** (ID 1860) -- one dashboard, per-host dropdown via
  the `alias` label set in `prometheus.yml`.
- **Cadvisor exporter** (ID 14282) -- per-container CPU/mem/network for the
  Immich docker stack.
- **Proxmox via Prometheus** (ID 10347) -- host-level PVE metrics (CPU,
  memory, per-VM/CT status) from `pve-exporter`.

## Deploy / redeploy

```
./deploy.sh
```

Stages `docker-compose.yml`, `prometheus.yml`, `alert_rules.yml`,
`alertmanager.yml` to the Proxmox host then `pct push`es them into CT104
(same pattern as `ops-agent/deploy.sh` -- no direct filesystem route from
the workstation into an LXC). `pve.yml` is generated from
`pve.yml.template` + the gitignored `pve_token` file rather than committed
with the real token in it. Then runs `docker compose up -d` inside the LXC.

Node exporter / cAdvisor installs on the actual target VMs are **not**
part of `deploy.sh` (they're one-time host-level installs, not part of the
monitoring stack's own config) -- see the table above for how each was set
up.

## Known scope cuts (not bugs, deliberate for this first pass)

- Alertmanager doesn't notify anywhere yet (see above).
- VM100 (HAOS) has no host-level metrics (see above) -- architectural
  limitation of the appliance, not a gap to fix with more effort.
- ~~Grafana's admin password is still the default set at creation~~ --
  changed 2026-08-26.
