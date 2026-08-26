#!/bin/bash
# Deploy ops-agent/ to LXC 103 (ops-agent) via the Proxmox host.
#
# The workstation has no direct route into the LXC's filesystem -- only
# `pct push` run on the Proxmox host itself can write into it -- so every
# file is staged to the host first, then pushed into the container.
#
# Usage: ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

REMOTE_DIR=/home/opsagent/ops-agent
STAGE=/tmp/ops-agent-deploy
PY_FILES="fetch_proxmox_log.py fetch_immich_log.py triage.py notify.py run_all.py verify_with_cloud.py"
SECRET_FILES="webhook_url openai_api_key"

ssh proxmox "mkdir -p $STAGE"
ssh proxmox "pct exec 103 -- install -d -o opsagent -g opsagent $REMOTE_DIR"

for f in $PY_FILES $SECRET_FILES ops-triage.service ops-triage.timer; do
  scp -q "$f" "proxmox:$STAGE/$f"
done

for f in $PY_FILES $SECRET_FILES; do
  ssh proxmox "pct push 103 $STAGE/$f $REMOTE_DIR/$f && pct exec 103 -- chown opsagent:opsagent $REMOTE_DIR/$f"
done
for f in $SECRET_FILES; do
  ssh proxmox "pct exec 103 -- chmod 600 $REMOTE_DIR/$f"
done
for f in $PY_FILES; do
  ssh proxmox "pct exec 103 -- chmod +x $REMOTE_DIR/$f"
done

ssh proxmox "pct push 103 $STAGE/ops-triage.service /etc/systemd/system/ops-triage.service"
ssh proxmox "pct push 103 $STAGE/ops-triage.timer /etc/systemd/system/ops-triage.timer"
ssh proxmox "pct exec 103 -- systemctl daemon-reload"
ssh proxmox "pct exec 103 -- systemctl enable --now ops-triage.timer"

ssh proxmox "rm -rf $STAGE"

echo "deployed."
echo "check with: ssh proxmox \"pct exec 103 -- systemctl status ops-triage.timer\""
echo "manual run: ssh proxmox \"pct exec 103 -- su - opsagent -c 'cd ops-agent && ./run_all.py'\""
