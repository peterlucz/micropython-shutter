#!/bin/bash
# Deploy the Prometheus/Grafana/Alertmanager/pve-exporter stack to LXC 104
# (monitoring) via the Proxmox host, and (re)start it with docker compose.
#
# Same staging pattern as ops-agent/deploy.sh: the workstation has no direct
# route into the LXC's filesystem -- only `pct push` run on the Proxmox host
# itself can write into it -- so every file is staged to the host first,
# then pushed into the container.
#
# Usage: ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

REMOTE_DIR=/opt/monitoring
STAGE=/tmp/monitoring-deploy
FILES="docker-compose.yml prometheus.yml alert_rules.yml alertmanager.yml alert_bridge.py"
SECRET_FILES="webhook_url"

ssh proxmox "mkdir -p $STAGE"
ssh proxmox "pct exec 104 -- install -d $REMOTE_DIR"

for f in $FILES $SECRET_FILES; do
  scp -q "$f" "proxmox:$STAGE/$f"
  ssh proxmox "pct push 104 $STAGE/$f $REMOTE_DIR/$f"
done
ssh proxmox "pct exec 104 -- chmod 600 $REMOTE_DIR/webhook_url"
ssh proxmox "pct exec 104 -- chmod +x $REMOTE_DIR/alert_bridge.py"

# pve.yml holds the PVE API token -- generated from the template + the
# gitignored pve_token file rather than committed directly.
sed "s/__PVE_TOKEN__/$(cat pve_token)/" pve.yml.template > "/tmp/pve.yml.$$"
scp -q "/tmp/pve.yml.$$" "proxmox:$STAGE/pve.yml"
rm -f "/tmp/pve.yml.$$"
ssh proxmox "pct push 104 $STAGE/pve.yml $REMOTE_DIR/pve.yml && pct exec 104 -- chmod 644 $REMOTE_DIR/pve.yml"

# alert_bridge.py runs directly on the LXC (systemd), not in docker compose --
# it needs to be reachable from the Alertmanager container by the LXC's real
# IP (10.30.0.104:9099 in alertmanager.yml), same reason CT104's own
# node_exporter target can't be scraped via "localhost" either.
scp -q "alert-bridge.service" "proxmox:$STAGE/alert-bridge.service"
ssh proxmox "pct push 104 $STAGE/alert-bridge.service /etc/systemd/system/alert-bridge.service"
ssh proxmox "pct exec 104 -- systemctl daemon-reload"
ssh proxmox "pct exec 104 -- systemctl enable --now alert-bridge.service"

ssh proxmox "pct exec 104 -- bash -c 'cd $REMOTE_DIR && docker compose up -d'"
# prometheus.yml/alert_rules.yml/alertmanager.yml are bind-mounted files, not
# part of the image/command/volumes list docker-compose diffs against -- `up
# -d` alone won't reload them into an already-running container.
ssh proxmox "pct exec 104 -- bash -c 'cd $REMOTE_DIR && docker compose restart prometheus alertmanager'"
ssh proxmox "rm -rf $STAGE"

echo "deployed."
echo "Prometheus:   http://10.30.0.104:9090"
echo "Grafana:      http://10.30.0.104:3000"
echo "Alertmanager: http://10.30.0.104:9093"
echo "check alert-bridge with: ssh proxmox \"pct exec 104 -- systemctl status alert-bridge --no-pager\""
