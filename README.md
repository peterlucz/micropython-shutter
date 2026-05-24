# micropython-shutter

MicroPython firmware for automated window shutter control via MQTT. Runs on ESP8266, ESP32, PyBoard, and RP2 microcontrollers.

## Features

- Controls up to 4 electric shutters via GPIO relay pairs
- Receives position commands over MQTT (`shutter/a/set/0-3`)
- Publishes shutter state and connection statistics
- Fetches shutter configuration (relay pins, timing) from an HTTP endpoint at boot
- Async/non-blocking I/O throughout using `uasyncio`
- LED feedback: red = WiFi/broker loss, blue = message received

## Hardware

Each shutter requires two relay outputs (up/down). GPIO pins and timing are defined in the JSON config fetched at runtime — no redeployment needed to adjust timing or pin assignments.

## Project Structure

```
micropython/shutter/
├── main.py          # Main application: async task orchestration, relay control
├── mqtt_as.py       # Async MQTT client library
├── mqtt_local.py    # Platform config: broker address, LED pin mappings
├── secrets.py       # Credentials (not committed — see secrets.py.example)
├── secrets.py.example
└── lib/json/        # MicroPython JSON library
```

## Setup

1. Copy `secrets.py.example` to `secrets.py` and fill in your credentials:

```python
MQTT_USER = 'your_mqtt_user'
MQTT_PASSWORD = 'your_mqtt_password'
WIFI_SSID = 'your_wifi_ssid'
WIFI_PASSWORD = 'your_wifi_password'
```

2. Edit `mqtt_local.py` to set your MQTT broker address and adjust LED pins for your board.

3. Set `SHUTTERCFG` in `main.py` to the URL of your shutter configuration JSON:

```json
{
  "shutter": [
    { "up": 14, "down": 15, "time_up": 22, "time_down": 21, "position": 0 },
    ...
  ]
}
```

4. Deploy to the microcontroller using [mpremote](https://docs.micropython.org/en/latest/reference/mpremote.html):

```bash
cd micropython/shutter
mpremote connect /dev/ttyACM0 cp secrets.py :secrets.py + cp main.py :main.py + cp mqtt_local.py :mqtt_local.py + cp mqtt_as.py :mqtt_as.py
mpremote connect /dev/ttyACM0 reset
```

## MQTT Interface

| Topic | Direction | Payload |
|---|---|---|
| `shutter/a/set/<id>` | Subscribe | `{"brightness": 0-100}` or `{"state": "ON"/"OFF"}` |
| `shutter/a/state/<id>` | Publish | `{"state": "ON"/"OFF", "brightness": 0-100}` |
| `shutter/a/state` | Publish | Connection statistics (every 5s) |

Position 0 = fully closed, 100 = fully open. Movement duration is calculated proportionally from the configured full-travel time.

## Dependencies

- [mqtt_as](https://github.com/peterhinch/micropython-mqtt) — async MQTT client by Peter Hinch
