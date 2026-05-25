#!/bin/bash
# update_config.sh — Publish a new devices.json config to a running Pico via MQTT.
#
# Usage: ./update_config.sh [0-4]
#
# Configs (8 relays on GPIO 14-21):
#   v0 — 0 shutters, 8 switches
#   v1 — 1 shutter,  6 switches
#   v2 — 2 shutters, 4 switches
#   v3 — 3 shutters, 2 switches  (default factory layout)
#   v4 — 4 shutters, 0 switches
#
# After publishing, the Pico saves the new config to devices.json on flash.
# Reboot the Pico to apply a new device layout (relay pin / type changes).
# Timing changes (time_up / time_down) take effect after reboot as well.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
info() { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
die()  { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

# ── read credentials and broker from project files ────────────────────────────
cd "$SCRIPT_DIR"
[ -f secrets.py ]   || die "secrets.py not found in $SCRIPT_DIR"
[ -f config.py ]    || die "config.py not found in $SCRIPT_DIR"

BROKER=$(python3 -c "exec(open('config.py').read()); print(MQTT_SERVER)")
MQTT_USER=$(python3 -c "exec(open('secrets.py').read()); print(MQTT_USER)")
MQTT_PASS=$(python3 -c "exec(open('secrets.py').read()); print(MQTT_PASSWORD)")

# ── config definitions ────────────────────────────────────────────────────────
# Relay layout:  shutter = 2 relays (up + down), switch = 1 relay
# GPIO 14-21 (8 relays total):  shutters use consecutive pairs from the left.
# time_up / time_down = ms for full travel (0→100%).  Calibrate per installation.
# duration = ms auto-off for timed switches; omit for manual switches.

CONFIG_0='{
  "devices": [
    {"id": 0, "type": "switch", "relay": 14},
    {"id": 1, "type": "switch", "relay": 15},
    {"id": 2, "type": "switch", "relay": 16},
    {"id": 3, "type": "switch", "relay": 17},
    {"id": 4, "type": "switch", "relay": 18},
    {"id": 5, "type": "switch", "relay": 19},
    {"id": 6, "type": "switch", "relay": 20},
    {"id": 7, "type": "switch", "relay": 21, "duration": 5000}
  ]
}'

CONFIG_1='{
  "devices": [
    {"id": 0, "type": "shutter", "relay_up": 14, "relay_down": 15, "time_up": 25000, "time_down": 25000, "position": 0},
    {"id": 1, "type": "switch", "relay": 16},
    {"id": 2, "type": "switch", "relay": 17},
    {"id": 3, "type": "switch", "relay": 18},
    {"id": 4, "type": "switch", "relay": 19},
    {"id": 5, "type": "switch", "relay": 20},
    {"id": 6, "type": "switch", "relay": 21, "duration": 5000}
  ]
}'

CONFIG_2='{
  "devices": [
    {"id": 0, "type": "shutter", "relay_up": 14, "relay_down": 15, "time_up": 25000, "time_down": 25000, "position": 0},
    {"id": 1, "type": "shutter", "relay_up": 16, "relay_down": 17, "time_up": 25000, "time_down": 25000, "position": 0},
    {"id": 2, "type": "switch", "relay": 18},
    {"id": 3, "type": "switch", "relay": 19},
    {"id": 4, "type": "switch", "relay": 20},
    {"id": 5, "type": "switch", "relay": 21, "duration": 5000}
  ]
}'

CONFIG_3='{
  "devices": [
    {"id": 0, "type": "shutter", "relay_up": 14, "relay_down": 15, "time_up": 25000, "time_down": 25000, "position": 0},
    {"id": 1, "type": "shutter", "relay_up": 16, "relay_down": 17, "time_up": 25000, "time_down": 25000, "position": 0},
    {"id": 2, "type": "shutter", "relay_up": 18, "relay_down": 19, "time_up": 25000, "time_down": 25000, "position": 0},
    {"id": 3, "type": "switch", "relay": 20},
    {"id": 4, "type": "switch", "relay": 21, "duration": 5000}
  ]
}'

CONFIG_4='{
  "devices": [
    {"id": 0, "type": "shutter", "relay_up": 14, "relay_down": 15, "time_up": 25000, "time_down": 25000, "position": 0},
    {"id": 1, "type": "shutter", "relay_up": 16, "relay_down": 17, "time_up": 25000, "time_down": 25000, "position": 0},
    {"id": 2, "type": "shutter", "relay_up": 18, "relay_down": 19, "time_up": 25000, "time_down": 25000, "position": 0},
    {"id": 3, "type": "shutter", "relay_up": 20, "relay_down": 21, "time_up": 25000, "time_down": 25000, "position": 0}
  ]
}'

# ── menu ──────────────────────────────────────────────────────────────────────
show_menu() {
    echo ""
    echo -e "${BOLD}Select a device layout (8 relays on GPIO 14–21):${NC}"
    echo ""
    echo "  0)  0 shutters  8 switches  — all switches (7 manual + 1 timed)"
    echo "  1)  1 shutter   6 switches  — 1 timed switch"
    echo "  2)  2 shutters  4 switches  — 1 timed switch"
    echo "  3)  3 shutters  2 switches  — 1 timed switch  [current factory default]"
    echo "  4)  4 shutters  0 switches"
    echo ""
}

# ── select config ─────────────────────────────────────────────────────────────
if [ $# -ge 1 ]; then
    CHOICE="$1"
else
    show_menu
    read -rp "Choice [0-4]: " CHOICE
fi

case "$CHOICE" in
    0) PAYLOAD="$CONFIG_0"; DESC="0 shutters, 8 switches" ;;
    1) PAYLOAD="$CONFIG_1"; DESC="1 shutter, 6 switches" ;;
    2) PAYLOAD="$CONFIG_2"; DESC="2 shutters, 4 switches" ;;
    3) PAYLOAD="$CONFIG_3"; DESC="3 shutters, 2 switches" ;;
    4) PAYLOAD="$CONFIG_4"; DESC="4 shutters, 0 switches" ;;
    *) die "Invalid choice '$CHOICE'. Must be 0–4." ;;
esac

# ── confirm ───────────────────────────────────────────────────────────────────
echo ""
info "Selected: v${CHOICE} — ${DESC}"
echo ""
echo "$PAYLOAD"
echo ""
read -rp "Publish this config to pico/config on broker ${BROKER}? [y/N] " CONFIRM
[[ "$CONFIRM" =~ ^[Yy]$ ]] || { warn "Aborted."; exit 0; }

# ── publish ───────────────────────────────────────────────────────────────────
mosquitto_pub \
    -h "$BROKER" \
    -u "$MQTT_USER" \
    -P "$MQTT_PASS" \
    -t "pico/config" \
    -r \
    -m "$PAYLOAD"

ok "Published to pico/config (retain=true)"
echo ""
warn "The Pico will save the new config to devices.json on flash."
warn "Reboot the Pico to apply the new layout:  mpremote reset"
