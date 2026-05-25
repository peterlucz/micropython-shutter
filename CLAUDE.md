# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository contains MicroPython firmware for automated window shutter and switch control via MQTT. The application runs on a **Raspberry Pi Pico W** with a **Waveshare Pico-Relay-B** board (8 relays on GPIO 14–21) and integrates with **Home Assistant** via MQTT Cover and Switch entities.

## Architecture

The application uses an **asynchronous event-driven architecture** with the following flow:

1. **Initialization**: Loads device config from `devices.json` on flash, connects to WiFi and MQTT broker
2. **Message Processing**: Three concurrent async tasks:
   - `messages()` — dispatches incoming MQTT commands to the correct device handler; also handles `pico/config` updates
   - `up()` — resubscribes to all device topics on every reconnect
   - `down()` — tracks connectivity loss and increments outage counter
3. **Device Control**: Per-device async tasks (`shutter_move`, `switch_toggle`) drive GPIO relay pins; tracked in `active_tasks` so they can be cancelled (e.g. STOP command)
4. **State Publishing**: Publishes device state after each move and periodically publishes connection stats to `pico/status`

### Device Types

| Type | Relays | MQTT topics | Notes |
|------|--------|-------------|-------|
| `shutter` | 2 (up + down) | `shutter/{id}/set`, `shutter/{id}/set_position`, `shutter/{id}/state` | Mutual exclusion enforced; position estimated on STOP |
| `switch` | 1 | `switch/{id}/set`, `switch/{id}/state` | Optional `duration` ms for auto-off |

### Key Design Patterns

- **uasyncio-based**: All I/O operations are non-blocking
- **Queue-based messaging**: MQTT messages are queued and processed asynchronously
- **Config from flash**: Device layout (relay pins, timing, type) stored in `devices.json`; updated via `pico/config` MQTT topic with `retain=True`
- **Position persistence**: Shutter positions written to `devices.json` after every move so they survive reboots
- **Task cancellation**: `cancel_active()` cancels any running move before starting a new one; STOP command estimates current position from elapsed time
- **Relay timing**: `move_time = time_up_or_down_ms * position_delta / 100`

### MQTT Topic Reference

| Topic | Direction | Payload |
|-------|-----------|---------|
| `shutter/{id}/set` | HA → Pico | `OPEN` / `CLOSE` / `STOP` |
| `shutter/{id}/set_position` | HA → Pico | `0`–`100` |
| `shutter/{id}/state` | Pico → HA | `{"state": "open", "position": 75}` |
| `switch/{id}/set` | HA → Pico | `ON` / `OFF` |
| `switch/{id}/state` | Pico → HA | `ON` / `OFF` |
| `pico/config` | HA → Pico | Full `devices.json` JSON (retain=True) |
| `pico/status` | Pico → HA | Heartbeat string every 30 s |

## Key Files

- **`micropython/shutter/main.py`** — Main application: config loading, async tasks, device handlers
- **`micropython/shutter/config.py`** — All deployment parameters: MQTT server IP, topic names, timing constants
- **`micropython/shutter/devices.json`** — Device layout stored on Pico flash; edit before first deploy
- **`micropython/shutter/mqtt_local.py`** — Pico W WiFi/MQTT connection setup and LED definitions
- **`micropython/shutter/secrets.py`** — WiFi and MQTT credentials (not committed; see `secrets.py.example`)
- **`micropython/shutter/mqtt_as.py`** — Third-party async MQTT client library (~1100 lines, do not modify)

## Configuration

### `config.py` — deployment parameters
```python
MQTT_SERVER  = '192.168.1.5'   # MQTT broker IP
STATUS_TOPIC = 'pico/status'   # heartbeat topic
CONFIG_TOPIC = 'pico/config'   # retained config topic
DEVICES_FILE = 'devices.json'  # config file on Pico flash
KEEPALIVE    = 120
QUEUE_LEN    = 1
DEBUG        = True
```

### `devices.json` — device layout
```json
{
  "devices": [
    {"id": 0, "type": "shutter", "relay_up": 14, "relay_down": 15,
     "time_up": 30000, "time_down": 30000, "position": 0},
    {"id": 1, "type": "switch", "relay": 20},
    {"id": 2, "type": "switch", "relay": 21, "duration": 5000}
  ]
}
```
`time_up` / `time_down` are milliseconds for full travel (0 → 100%). `duration` is milliseconds for auto-off switches.

### Updating config from Home Assistant
Publish the updated JSON to `pico/config` with **retain=True** in HA Developer Tools → MQTT. The Pico saves it to `devices.json` on receipt; reboot to apply a new device layout. Position values in the incoming config are ignored — live positions are preserved automatically.

## Development Notes

### Deployment

Files to upload to the Pico with `mpremote`:
```bash
mpremote cp config.py secrets.py mqtt_local.py main.py mqtt_as.py devices.json :
```

For quick iteration without flashing:
```bash
mpremote run main.py
```

### Testing

Monitor serial output in one terminal:
```bash
mpremote
```

Subscribe to all MQTT traffic in another:
```bash
mosquitto_sub -h 192.168.1.5 -t '#' -v
```

Send test commands:
```bash
mosquitto_pub -h 192.168.1.5 -t shutter/0/set_position -m 50
mosquitto_pub -h 192.168.1.5 -t shutter/0/set -m STOP
mosquitto_pub -h 192.168.1.5 -t switch/1/set -m ON
```

### Home Assistant Cover entity example
```yaml
cover:
  - platform: mqtt
    name: "Shutter 0"
    command_topic: "shutter/0/set"
    set_position_topic: "shutter/0/set_position"
    state_topic: "shutter/0/state"
    value_template: "{{ value_json.state }}"
    position_topic: "shutter/0/state"
    position_template: "{{ value_json.position }}"
```

### Async Programming with uasyncio

- Use `await asyncio.sleep()` / `asyncio.sleep_ms()` for delays (not `time.sleep()`)
- Use `await client.subscribe()`, `await client.publish()` for MQTT operations
- Create concurrent tasks with `asyncio.create_task()`
- Cancel tasks with `task.cancel()` + `await task` inside a try/except

### MicroPython-Specific Considerations

- Use `ujson` instead of standard `json`
- Use `uasyncio` instead of standard `asyncio` (imported as `asyncio`)
- Use `machine.Pin` for GPIO control
- `asyncio.CancelledError` is available in MicroPython 1.20+
- File I/O (`open`, `ujson.dump`) is synchronous but fast enough for small files

### Common Modifications

- **Add a device**: Add an entry to `devices.json` and publish to `pico/config` with retain, or edit the file and redeploy
- **Change relay pins or timing**: Edit `devices.json` (or publish new config via MQTT)
- **Change MQTT broker or topics**: Edit `config.py`
- **Change WiFi or MQTT credentials**: Edit `secrets.py`
