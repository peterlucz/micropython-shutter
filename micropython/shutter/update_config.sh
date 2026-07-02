#!/bin/bash
# update_config.sh — Publish a devices.json config file to a running Pico via MQTT.
#
# Usage: ./update_config.sh <config-file> [device-id]
#
# device-id is the MAC-derived board id (e.g. pico_relay_ab12cd) that prefixes
# all of the board's MQTT topics. If omitted, the script discovers running
# boards from their retained '{device-id}/status' topics; with exactly one
# board on the broker it is picked automatically.
#
# Examples:
#   ./update_config.sh devices.json.example3
#   ./update_config.sh my_custom_layout.json pico_relay_ab12cd
#
# Example files (8 relays on GPIO 14-21):
#   devices.json.example0  —  0 shutters, 8 switches
#   devices.json.example1  —  1 shutter,  6 switches
#   devices.json.example2  —  2 shutters, 4 switches
#   devices.json.example3  —  3 shutters, 2 switches  (default factory layout)
#   devices.json.example4  —  4 shutters, 0 switches
#
# After publishing, the Pico saves the new config to devices.json on flash.
# Reboot the Pico to apply layout changes:  mpremote reset

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
info() { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
die()  { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

# ── usage ─────────────────────────────────────────────────────────────────────
if [ $# -lt 1 ] || [ $# -gt 2 ]; then
    echo "Usage: $0 <config-file> [device-id]"
    echo ""
    echo "Available example files:"
    for f in "$SCRIPT_DIR"/devices.json.example*; do
        [ -f "$f" ] || continue
        name=$(basename "$f")
        shutters=$(python3 -c "
import json
d = json.load(open('$f'))['devices']
s = sum(1 for x in d if x['type']=='shutter')
w = sum(1 for x in d if x['type']=='switch')
print('{} shutter{}, {} switch{}'.format(s, 's' if s!=1 else '', w, 'es' if w!=1 else ''))
" 2>/dev/null || echo "?")
        printf "  %-30s  %s\n" "$name" "$shutters"
    done
    exit 1
fi

CONFIG_FILE="$1"
DEVICE_ID="${2:-}"

# Resolve relative paths against the script directory if the file isn't found as-is
if [ ! -f "$CONFIG_FILE" ]; then
    CONFIG_FILE="$SCRIPT_DIR/$1"
fi
[ -f "$CONFIG_FILE" ] || die "File not found: $1"

# ── validate JSON ─────────────────────────────────────────────────────────────
python3 -c "import json; json.load(open('$CONFIG_FILE'))" 2>/dev/null \
    || die "$CONFIG_FILE is not valid JSON"

# ── read credentials and broker ───────────────────────────────────────────────
cd "$SCRIPT_DIR"
[ -f secrets.py ] || die "secrets.py not found in $SCRIPT_DIR"
[ -f config.py ]  || die "config.py not found in $SCRIPT_DIR"

BROKER=$(python3 -c "exec(open('config.py').read()); print(MQTT_SERVER)")
MQTT_USER=$(python3 -c "exec(open('secrets.py').read()); print(MQTT_USER)")
MQTT_PASS=$(python3 -c "exec(open('secrets.py').read()); print(MQTT_PASSWORD)")

# ── device id ─────────────────────────────────────────────────────────────────
if [ -z "$DEVICE_ID" ]; then
    info "No device id given — discovering boards via retained status topics..."
    mapfile -t IDS < <(mosquitto_sub -h "$BROKER" -u "$MQTT_USER" -P "$MQTT_PASS" \
        -t '+/status' -W 2 -F '%t' 2>/dev/null | sed 's|/status$||' | sort -u)
    case ${#IDS[@]} in
        0) die "No boards found on $BROKER. Pass the device id explicitly: $0 $1 <device-id>" ;;
        1) DEVICE_ID="${IDS[0]}"
           ok "Found one board: $DEVICE_ID" ;;
        *) warn "Multiple boards found:"
           printf '  %s\n' "${IDS[@]}"
           die "Pass the device id explicitly: $0 $1 <device-id>" ;;
    esac
fi
TOPIC="$DEVICE_ID/config"

# ── confirm ───────────────────────────────────────────────────────────────────
echo ""
info "Config file : $(basename "$CONFIG_FILE")"
info "Broker      : $BROKER"
info "Device      : $DEVICE_ID"
echo ""
cat "$CONFIG_FILE"
echo ""
read -rp "Publish to ${TOPIC} on broker ${BROKER}? [y/N] " CONFIRM
[[ "$CONFIRM" =~ ^[Yy]$ ]] || { warn "Aborted."; exit 0; }

# ── publish ───────────────────────────────────────────────────────────────────
mosquitto_pub \
    -h "$BROKER" \
    -u "$MQTT_USER" \
    -P "$MQTT_PASS" \
    -t "$TOPIC" \
    -r \
    -f "$CONFIG_FILE"

ok "Published $(basename "$CONFIG_FILE") to $TOPIC (retain=true)"
echo ""
warn "The Pico will save the new config to devices.json on flash."
warn "Reboot the Pico to apply the new layout:  mpremote reset"
