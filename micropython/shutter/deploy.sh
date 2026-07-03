#!/bin/bash
# deploy.sh — Flash MicroPython and deploy shutter firmware to a brand-new Pico W.
#
# Usage: ./deploy.sh
#
# What it does:
#   1. Checks dependencies (mpremote, curl/wget)
#   2. Creates secrets.py interactively if it doesn't exist
#   3. Downloads the latest MicroPython Pico W firmware (cached in ~/.cache/pico-firmware)
#   4. Prompts you to put the Pico in BOOTSEL mode, then flashes the UF2
#   5. Waits for the Pico to reboot as a serial device
#   6. Uploads all application files via mpremote
#   7. Resets the Pico to start the app

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_FILES="config.py mqtt_local.py main.py mqtt_as.py"
FIRMWARE_URL="https://micropython.org/download/RPI_PICO_W/RPI_PICO_W-latest.uf2"
FIRMWARE_CACHE="$HOME/.cache/pico-firmware/RPI_PICO_W-latest.uf2"

# ── colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
ok()    { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
step()  { echo -e "\n${BOLD}=== $* ===${NC}"; }
die()   { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

# ── Step 1: dependencies ──────────────────────────────────────────────────────
step "Checking dependencies"
command -v mpremote >/dev/null 2>&1 || die "mpremote not found. Install with: pip install mpremote"
ok "mpremote found"

if command -v curl >/dev/null 2>&1; then
    DOWNLOAD="curl -fsSL -o"
elif command -v wget >/dev/null 2>&1; then
    DOWNLOAD="wget -q -O"
else
    die "Neither curl nor wget found. Please install one."
fi

# ── Step 2: secrets.py ────────────────────────────────────────────────────────
step "Checking secrets.py"
SECRETS="$SCRIPT_DIR/secrets.py"
if [ -f "$SECRETS" ]; then
    ok "secrets.py already exists — skipping"
else
    warn "secrets.py not found. Enter credentials to create it."
    echo ""
    read -rp "  WiFi SSID       : " WIFI_SSID
    read -rsp "  WiFi password   : " WIFI_PASSWORD; echo
    read -rp "  MQTT username   : " MQTT_USER
    read -rsp "  MQTT password   : " MQTT_PASSWORD; echo
    cat > "$SECRETS" <<EOF
WIFI_SSID = '$WIFI_SSID'
WIFI_PASSWORD = '$WIFI_PASSWORD'
MQTT_USER = '$MQTT_USER'
MQTT_PASSWORD = '$MQTT_PASSWORD'
EOF
    ok "Created secrets.py"
fi

# ── Step 3: firmware ──────────────────────────────────────────────────────────
step "Checking MicroPython firmware"
mkdir -p "$(dirname "$FIRMWARE_CACHE")"
if [ -f "$FIRMWARE_CACHE" ]; then
    ok "Firmware already cached: $FIRMWARE_CACHE"
else
    info "Downloading latest MicroPython for Pico W..."
    $DOWNLOAD "$FIRMWARE_CACHE" "$FIRMWARE_URL" || die "Download failed"
    ok "Downloaded: $FIRMWARE_CACHE"
fi

# ── Step 4: flash firmware ────────────────────────────────────────────────────
step "Flashing MicroPython firmware"
echo ""
warn "Put the Pico W into BOOTSEL mode:"
warn "  1. Hold the BOOTSEL button on the Pico W"
warn "  2. Plug in the USB cable (while holding BOOTSEL)"
warn "  3. Release BOOTSEL"
warn "  4. The Pico should appear as a drive called RPI-RP2"
echo ""
read -rp "Press Enter when the RPI-RP2 drive is visible..."

# Locate the mount point
MOUNT=""
for candidate in \
    "/media/$USER/RPI-RP2" \
    "/media/RPI-RP2" \
    "/mnt/RPI-RP2" \
    "/run/media/$USER/RPI-RP2" \
    "/Volumes/RPI-RP2"
do
    if [ -d "$candidate" ]; then
        MOUNT="$candidate"
        break
    fi
done

if [ -z "$MOUNT" ]; then
    # Try a broader search
    MOUNT=$(find /media /mnt /run/media /Volumes -maxdepth 3 -name "RPI-RP2" -type d 2>/dev/null | head -1 || true)
fi

[ -z "$MOUNT" ] && die "RPI-RP2 drive not found. Check it's mounted and try again."
ok "Found RPI-RP2 at: $MOUNT"

info "Copying firmware..."
cp "$FIRMWARE_CACHE" "$MOUNT/"
ok "Firmware flashed. Pico is rebooting..."

# ── Step 5: wait for serial device ────────────────────────────────────────────
step "Waiting for Pico to reboot"
sleep 3
READY=0
for i in $(seq 1 20); do
    if mpremote ls > /dev/null 2>&1; then
        READY=1
        break
    fi
    echo -n "."
    sleep 1
done
echo ""
[ "$READY" -eq 0 ] && die "Pico did not appear as a serial device after 20s. Check USB connection."
ok "Pico is ready"

# ── Step 6: upload files ──────────────────────────────────────────────────────
step "Uploading application files"
cd "$SCRIPT_DIR"
for f in $APP_FILES secrets.py; do
    mpremote cp "$f" ":$f"
    ok "  $f"
done

# devices.json holds live shutter positions (and MicroPython re-flashes keep
# the filesystem), so seed it only on a fresh board — never clobber it on a
# re-deploy.
if mpremote exec "import os; os.stat('devices.json')" >/dev/null 2>&1; then
    warn "  devices.json already on the board — kept (holds live positions)"
else
    mpremote cp devices.json ":devices.json"
    ok "  devices.json (seeded)"
fi

# ── Step 7: done ──────────────────────────────────────────────────────────────
step "Done"
ok "All files uploaded. Resetting Pico..."
mpremote reset
echo ""
echo -e "${BOLD}Deployment complete!${NC}"
echo "The Pico will now connect to WiFi and the MQTT broker."
echo "Watch the serial output with:  mpremote"
