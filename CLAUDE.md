# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository contains MicroPython firmware for automated window shutter control via MQTT. The application runs on embedded platforms (ESP8266, ESP32, PyBoard, RP2) and manages multiple electric shutters through relay control, responding to commands received over MQTT.

## Architecture

The application uses an **asynchronous event-driven architecture** with the following flow:

1. **Initialization**: Connects to WiFi and MQTT broker, fetches shutter configuration from HTTP endpoint (`SHUTTERCFG` URL)
2. **Message Processing**: Asynchronous tasks handle three concurrent streams:
   - `messages()` - Receives position commands from MQTT topics (`shutter/a/set/0-3`)
   - `up()` - Monitors connection state, resubscribes on reconnect
   - `down()` - Tracks connectivity loss and increments outage counter
3. **Relay Control**: `relay_timer()` tasks pulse GPIO relay pins to move shutters for calculated durations
4. **State Publishing**: Periodically publishes shutter positions and connection stats to `shutter/a/state`

### Key Design Patterns

- **uasyncio-based**: All I/O operations are non-blocking using Python's asyncio
- **Queue-based messaging**: MQTT messages are queued and processed asynchronously
- **Configuration from HTTP**: Shutter parameters (timing, relay pins) loaded from external JSON endpoint
- **GPIO relay timing**: Shutter position calculated by pulse duration: `relay_time = full_time * (position_delta / 100) * 10ms`
- **Event-driven LED feedback**: Red LED indicates WiFi/MQTT loss, blue LED pulses on message receive

## Key Files

- **`micropython/shutter/main.py`** - Main application entry point with async task orchestration
- **`micropython/shutter/mqtt_as.py`** - MQTT client library (async version, ~1100 lines)
- **`micropython/shutter/mqtt_local.py`** - Platform-specific configuration (WiFi SSID, MQTT broker, LED pin definitions)
- **`micropython/shutter/lib/json/`** - JSON serialization library for MicroPython platforms

## Development Notes

### Deployment

These are raw Python files deployed directly to a microcontroller's filesystem. There is no build system. To develop:

1. Modify `.py` files as needed
2. Upload changed files to the microcontroller's filesystem using a MicroPython upload tool (e.g., `ampy`, `mpremote`, or WebREPL)
3. The microcontroller runs `main.py` on boot via the boot sequence

### Configuration

`mqtt_local.py` contains:
- MQTT broker address, username, password
- WiFi SSID and password
- Platform-specific LED pin mappings (active-high/active-low handling per platform)

The shutter configuration itself (relay pins, timing per shutter) is fetched from an HTTP endpoint during runtime and not stored in code.

### Async Programming with uasyncio

All I/O is asynchronous:
- Use `await asyncio.sleep()` for delays (not `time.sleep()`)
- Use `await client.subscribe()`, `await client.publish()` for MQTT operations
- Create concurrent tasks with `asyncio.create_task()`
- Handle multiple simultaneous relay timers via task creation in the message handler

### MicroPython-Specific Considerations

- Use `ujson` (MicroPython's JSON) instead of standard `json`
- Use `uasyncio` instead of standard `asyncio`
- Use `urequests` instead of `requests` for HTTP
- Use `machine` module for GPIO control (not RPi.GPIO or similar)
- Memory is constrained; `gc.collect()` calls are used in `mqtt_as.py` to manage heap

### Common Modifications

- **Change MQTT topics**: Edit the hardcoded topic strings in `main.py` (`TOPIC` constant and `subscribe()` calls)
- **Change shutter count**: Modify the loop that subscribes to `shutter/a/set/X` topics and the corresponding GPIO ranges
- **Adjust relay pins**: The shutter configuration JSON determines which GPIO pins control which shutters
- **Change platforms**: Platform detection is done in `mqtt_local.py` via `sys.platform` checks

## Testing Strategy

There is no formal test framework. To verify changes:

1. Deploy the modified code to a test microcontroller
2. Monitor MQTT traffic using `mosquitto_sub` or similar
3. Send test commands via `mosquitto_pub` to verify relay control
4. Check serial output via the microcontroller's REPL for debug messages

The code includes `MQTTClient.DEBUG = True` for detailed logging.
