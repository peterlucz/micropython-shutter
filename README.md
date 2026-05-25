# micropython-shutter

MicroPython firmware for automated window shutter and switch control via MQTT. Runs on a **Raspberry Pi Pico W** with a **Waveshare Pico-Relay-B** board (8 relays on GPIO 14–21) and integrates with **Home Assistant** via MQTT Discovery — no YAML needed.

## Features

- Controls up to 4 shutters and up to 8 switches (configurable mix across 8 relays)
- Full shutter control: open, close, stop, set position (0–100%)
- Position estimated and saved to flash on every move and stop — survives reboots
- MQTT Discovery: Home Assistant creates Cover and Switch entities automatically on boot
- Each Pico W gets a unique device ID derived from its WiFi MAC address — safe to run multiple boards in the same HA instance
- Device config (relay pins, timing, type) stored on flash and updatable via MQTT without redeployment
- Async/non-blocking I/O throughout using `uasyncio`

## Hardware

- **Raspberry Pi Pico W**
- **Waveshare Pico-Relay-B** — 8 relays on GPIO 14–21

Each shutter uses two relays (up + down) with mutual exclusion enforced in firmware. Switches use one relay each, with optional auto-off duration.

## Project Structure

```
micropython/shutter/
├── main.py             # Application: async tasks, device handlers, MQTT logic
├── config.py           # Deployment parameters: broker IP, topic names
├── devices.json        # Device layout: relay pins, type, timing
├── mqtt_local.py       # Pico W WiFi/MQTT setup and LED definitions
├── mqtt_as.py          # Third-party async MQTT client library (do not modify)
├── secrets.py          # Credentials (not committed — see secrets.py.example)
├── secrets.py.example
├── deploy.sh           # First-time deploy script for a brand-new Pico W
└── update_config.sh    # Update device layout on a running Pico via MQTT
```

## Quick Start

### First-time install on a new Pico W

Run the deploy script — it handles everything:

```bash
cd micropython/shutter
./deploy.sh
```

The script will:
1. Create `secrets.py` interactively (WiFi + MQTT credentials) if it doesn't exist
2. Download the latest MicroPython firmware for Pico W (cached for future deploys)
3. Guide you through putting the Pico into BOOTSEL mode and flash the firmware
4. Upload all application files via `mpremote`
5. Reset the Pico to start the app

### Manual deploy (Pico already running MicroPython)

```bash
cd micropython/shutter
mpremote cp config.py secrets.py mqtt_local.py main.py mqtt_as.py devices.json :
mpremote reset
```

## Device Layout

The default layout (`devices.json`) is 3 shutters + 2 switches:

```json
{
  "devices": [
    {"id": 0, "type": "shutter", "relay_up": 14, "relay_down": 15, "time_up": 25000, "time_down": 25000, "position": 0},
    {"id": 1, "type": "shutter", "relay_up": 16, "relay_down": 17, "time_up": 25000, "time_down": 25000, "position": 0},
    {"id": 2, "type": "shutter", "relay_up": 18, "relay_down": 19, "time_up": 25000, "time_down": 25000, "position": 0},
    {"id": 3, "type": "switch",  "relay": 20},
    {"id": 4, "type": "switch",  "relay": 21, "duration": 5000}
  ]
}
```

`time_up` / `time_down` — milliseconds for full travel (0 → 100%). Calibrate per installation.  
`duration` — milliseconds for auto-off switches; omit for manual on/off.

### Changing the layout on a running Pico

Five preset layout files are included (8 relays, GPIO 14–21):

| File | Shutters | Switches |
|------|----------|----------|
| `devices.json.example0` | 0 | 8 |
| `devices.json.example1` | 1 | 6 |
| `devices.json.example2` | 2 | 4 |
| `devices.json.example3` | 3 | 2 — default |
| `devices.json.example4` | 4 | 0 |

Publish a preset to the running Pico:

```bash
./update_config.sh devices.json.example2
```

Running with no arguments lists the available files with a shutter/switch summary.

You can also pass any custom JSON file:

```bash
./update_config.sh my_layout.json
```

The Pico saves the new config to flash immediately. **Reboot the Pico** to apply a new device layout:

```bash
mpremote reset
```

Timing-only changes (`time_up` / `time_down`) also require a reboot to take effect.

For a custom layout, publish any valid `devices.json` JSON to the `pico/config` topic with `retain=true`:

```bash
mosquitto_pub -h <broker-ip> -u <user> -P <pass> -t pico/config -r -m '{"devices": [...]}'
```

## Home Assistant Setup

No YAML configuration needed. The Pico publishes MQTT Discovery messages on every boot.

1. Deploy and boot the Pico
2. In HA: **Settings → Devices & Services → MQTT** — the device appears as `Pico Relay XXXXXX` (last 3 bytes of its MAC address)
3. Cover and Switch entities are created automatically and grouped under one HA device

**Running multiple Picos**: no extra config needed — each board derives a unique device ID from its WiFi MAC address automatically.

## MQTT Topics

| Topic | Direction | Payload |
|-------|-----------|---------|
| `shutter/{id}/set` | HA → Pico | `OPEN` / `CLOSE` / `STOP` |
| `shutter/{id}/set_position` | HA → Pico | `0`–`100` |
| `shutter/{id}/state` | Pico → HA | `{"state": "open", "position": 75}` |
| `switch/{id}/set` | HA → Pico | `ON` / `OFF` |
| `switch/{id}/state` | Pico → HA | `ON` / `OFF` |
| `pico/config` | HA → Pico | Full `devices.json` JSON (retain=true) |
| `pico/status` | Pico → HA | `online` / `offline` |

## Configuration

Edit `config.py` to change the MQTT broker IP or topic names:

```python
MQTT_SERVER      = '192.168.1.5'   # MQTT broker IP
STATUS_TOPIC     = 'pico/status'
CONFIG_TOPIC     = 'pico/config'
DEVICES_FILE     = 'devices.json'
DISCOVERY_PREFIX = 'homeassistant'
```

The device ID and name (`pico_relay_XXXXXX` / `Pico Relay XXXXXX`) are derived automatically from the Pico W's WiFi MAC address at runtime — they are not in `config.py`.

## Dependencies

- [mqtt_as](https://github.com/peterhinch/micropython-mqtt) — async MQTT client by Peter Hinch (included as `mqtt_as.py`)
- [mpremote](https://docs.micropython.org/en/latest/reference/mpremote.html) — for deployment (`pip install mpremote`)
