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
FILES="docker-compose.yml prometheus.yml alert_rules.yml alertmanager.yml"

ssh proxmox "mkdir -p $STAGE"
ssh proxmox "pct exec 104 -- install -d $REMOTE_DIR"

for f in $FILES; do
  scp -q "$f" "proxmox:$STAGE/$f"
  ssh proxmox "pct push 104 $STAGE/$f $REMOTE_DIR/$f"
done

# pve.yml holds the PVE API token -- generated from the template + the
# gitignored pve_token file rather than committed directly.
sed "s/__PVE_TOKEN__/$(cat pve_token)/" pve.yml.template > "/tmp/pve.yml.$$"
scp -q "/tmp/pve.yml.$$" "proxmox:$STAGE/pve.yml"
rm -f "/tmp/pve.yml.$$"
ssh proxmox "pct push 104 $STAGE/pve.yml $REMOTE_DIR/pve.yml && pct exec 104 -- chmod 644 $REMOTE_DIR/pve.yml"

ssh proxmox "pct exec 104 -- bash -c 'cd $REMOTE_DIR && docker compose up -d'"
ssh proxmox "rm -rf $STAGE"

echo "deployed."
echo "Prometheus:   http://192.168.1.12:9090"
echo "Grafana:      http://192.168.1.12:3000  (admin / changeme -- change this)"
echo "Alertmanager: http://192.168.1.12:9093"
