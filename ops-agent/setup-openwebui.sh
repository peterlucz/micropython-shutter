#!/bin/bash
# One-time setup: Docker + Open WebUI on LXC 103, talking to the Ollama
# instance that's already running there natively (not containerized).
#
# Idempotent-ish: safe to re-run, but won't undo a prior `docker run`
# (stop/rm the old open-webui container first if you want a clean redeploy).
#
# Usage: ./setup-openwebui.sh
set -euo pipefail

ssh proxmox "pct set 103 --features nesting=1,keyctl=1"
ssh proxmox "pct reboot 103"
sleep 10
ssh proxmox "pct exec 103 -- systemctl is-active ollama"

ssh proxmox 'pct exec 103 -- bash -c "curl -fsSL https://get.docker.com | sh"'

ssh proxmox 'pct exec 103 -- docker run -d \
  --network host \
  -e OLLAMA_BASE_URL=http://127.0.0.1:11434 \
  -e WEBUI_AUTH=true \
  -v open-webui:/app/backend/data \
  --name open-webui \
  --restart always \
  ghcr.io/open-webui/open-webui:main'

echo "Open WebUI starting -- give it ~30s, then browse to http://10.30.0.103:8080"
