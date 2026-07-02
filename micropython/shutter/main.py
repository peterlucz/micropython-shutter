from mqtt_as import MQTTClient
from mqtt_local import wifi_led, blue_led, config
from config import DEVICES_FILE, DISCOVERY_PREFIX, KEEPALIVE, QUEUE_LEN, DEBUG
import uasyncio as asyncio
import machine
from machine import Pin
import network
import ujson
import utime
import gc

# Derived from the WiFi MAC address — e.g. 'pico_relay_ab12cd'. DEVICE_ID is
# also the MQTT topic namespace ('{DEVICE_ID}/shutter/0/set', …) so several
# boards can share one broker without their topics colliding.
_wlan = network.WLAN(network.STA_IF)
_wlan.active(True)
_suffix = ''.join('{:02x}'.format(b) for b in _wlan.config('mac')[-3:])
DEVICE_ID    = 'pico_relay_{}'.format(_suffix)
DEVICE_NAME  = 'Pico Relay {}'.format(_suffix.upper())
STATUS_TOPIC = '{}/status'.format(DEVICE_ID)
CONFIG_TOPIC = '{}/config'.format(DEVICE_ID)

# Keyed by device id, populated from devices.json on boot.
devices      = {}
active_tasks = {}
outages      = 0

# Keys that must not be written back to the JSON file.
_SKIP_KEYS = {'pin_up', 'pin_down', 'pin'}


# ---------------------------------------------------------------------------
# File-based config persistence
# ---------------------------------------------------------------------------

def load_config():
    with open(DEVICES_FILE) as f:
        return ujson.load(f)['devices']


def save_config():
    data = {'devices': [
        {k: v for k, v in d.items() if k not in _SKIP_KEYS}
        for d in sorted(devices.values(), key=lambda x: x['id'])
    ]}
    with open(DEVICES_FILE, 'w') as f:
        ujson.dump(data, f)


def apply_mqtt_config(payload):
    """Merge incoming MQTT config with live positions, then save to file."""
    new_data = ujson.loads(payload)
    for d in new_data.get('devices', []):
        if d.get('type') == 'shutter' and d['id'] in devices:
            d['position'] = devices[d['id']].get('position', 0)
    # The broker redelivers the retained config on every reconnect — skip
    # the write when nothing changed so flaky WiFi doesn't wear the flash.
    try:
        with open(DEVICES_FILE) as f:
            if ujson.load(f) == new_data:
                return
    except (OSError, ValueError):
        pass
    with open(DEVICES_FILE, 'w') as f:
        ujson.dump(new_data, f)
    print('Config updated on disk — reboot to apply new device layout.')


# ---------------------------------------------------------------------------
# State publishing
# ---------------------------------------------------------------------------

async def publish_shutter_state(device, state):
    payload = ujson.dumps({'state': state, 'position': device['position']})
    await client.publish('{}/shutter/{}/state'.format(DEVICE_ID, device['id']),
                         payload, qos=1)


async def publish_switch_state(device, state):
    await client.publish('{}/switch/{}/state'.format(DEVICE_ID, device['id']),
                         state, qos=1)


async def publish_discovery():
    dev_info = {'ids': [DEVICE_ID], 'name': DEVICE_NAME,
                'mdl': 'Waveshare Pico-Relay-B', 'mf': 'Waveshare'}
    for dev_id, device in devices.items():
        if device['type'] == 'shutter':
            topic = '{}/cover/{}_{}/config'.format(DISCOVERY_PREFIX, DEVICE_ID, dev_id)
            payload = ujson.dumps({
                'name':        'Shutter {}'.format(dev_id),
                'uniq_id':     '{}_{}_shutter_{}'.format(DEVICE_ID, DEVICE_NAME.replace(' ', '_'), dev_id),
                'cmd_t':       '{}/shutter/{}/set'.format(DEVICE_ID, dev_id),
                'set_pos_t':   '{}/shutter/{}/set_position'.format(DEVICE_ID, dev_id),
                'stat_t':      '{}/shutter/{}/state'.format(DEVICE_ID, dev_id),
                'val_tpl':     '{{ value_json.state }}',
                'pos_t':       '{}/shutter/{}/state'.format(DEVICE_ID, dev_id),
                'pos_tpl':     '{{ value_json.position }}',
                'avty_t':      STATUS_TOPIC,
                'pl_avail':    'online',
                'pl_not_avail':'offline',
                'pl_open':     'OPEN',
                'pl_cls':      'CLOSE',
                'pl_stop':     'STOP',
                'stat_open':   'open',
                'stat_clsd':   'closed',
                'stat_opening':'opening',
                'stat_closing':'closing',
                'stat_stopped':'stopped',
                'opt':         False,
                'ret':         False,
                'dev':         dev_info,
            })
        elif device['type'] == 'switch':
            topic = '{}/switch/{}_{}/config'.format(DISCOVERY_PREFIX, DEVICE_ID, dev_id)
            payload = ujson.dumps({
                'name':        'Switch {}'.format(dev_id),
                'uniq_id':     '{}_{}_switch_{}'.format(DEVICE_ID, DEVICE_NAME.replace(' ', '_'), dev_id),
                'cmd_t':       '{}/switch/{}/set'.format(DEVICE_ID, dev_id),
                'stat_t':      '{}/switch/{}/state'.format(DEVICE_ID, dev_id),
                'pl_on':       'ON',
                'pl_off':      'OFF',
                'avty_t':      STATUS_TOPIC,
                'pl_avail':    'online',
                'pl_not_avail':'offline',
                'dev':         dev_info,
            })
        else:
            continue
        await client.publish(topic, payload, retain=True, qos=1)
        gc.collect()


# ---------------------------------------------------------------------------
# Device coroutines
# ---------------------------------------------------------------------------

async def shutter_move(device, target):
    start = device['position']

    if target > start:
        pin_active = device['pin_up']
        pin_idle   = device['pin_down']
        travel     = device['time_up']
        direction  = 1
        move_state = 'opening'
    else:
        pin_active = device['pin_down']
        pin_idle   = device['pin_up']
        travel     = device['time_down']
        direction  = -1
        move_state = 'closing'

    if target == 0 or target == 100:
        # Re-home at the end stops: drive for the full travel time plus 10%
        # regardless of the believed position. The motor's limit switch stops
        # the shutter, so accumulated timing drift is cleared on every full
        # open/close instead of building up forever.
        move_time = travel * 11 // 10
    else:
        move_time = travel * abs(target - start) // 100

    # Safety: never activate if either relay in the pair is already on.
    if pin_active.value() or pin_idle.value():
        return

    t0 = utime.ticks_ms()
    try:
        await publish_shutter_state(device, move_state)
        pin_active.high()
        await asyncio.sleep_ms(move_time)
        pin_active.low()
        device['position'] = target
        save_config()
        end_state = 'closed' if target == 0 else 'open'
        await publish_shutter_state(device, end_state)
    except asyncio.CancelledError:
        pin_active.low()
        elapsed = utime.ticks_diff(utime.ticks_ms(), t0)
        # Estimate from the travel rate (% per ms), not the commanded move
        # time — re-homing moves run longer than the position delta implies.
        delta = round(elapsed * 100 / travel)
        device['position'] = max(0, min(100, start + direction * delta))
        save_config()
        await publish_shutter_state(device, 'stopped')
        raise


async def switch_toggle(device, state):
    pin = device['pin']
    if state == 'ON':
        pin.high()
        await publish_switch_state(device, 'ON')
        if 'duration' in device:
            try:
                await asyncio.sleep_ms(device['duration'])
                pin.low()
                await publish_switch_state(device, 'OFF')
            except asyncio.CancelledError:
                pin.low()
                await publish_switch_state(device, 'OFF')
                raise
    else:
        pin.low()
        await publish_switch_state(device, 'OFF')


# ---------------------------------------------------------------------------
# Task management
# ---------------------------------------------------------------------------

async def cancel_active(dev_id):
    task = active_tasks.get(dev_id)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        active_tasks[dev_id] = None


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def handle_shutter(device, command, payload):
    dev_id = device['id']

    if command == 'set_position':
        try:
            target = max(0, min(100, int(payload)))
        except (ValueError, TypeError):
            return
    elif command == 'set':
        cmd = payload.upper()
        if   cmd == 'OPEN':  target = 100
        elif cmd == 'CLOSE': target = 0
        elif cmd == 'STOP':
            await cancel_active(dev_id)
            return
        else:
            return
    else:
        return

    if target == device['position']:
        return

    await cancel_active(dev_id)
    active_tasks[dev_id] = asyncio.create_task(shutter_move(device, target))


async def handle_switch(device, command, payload):
    if command != 'set':
        return
    state = payload.upper()
    if state not in ('ON', 'OFF'):
        return
    dev_id = device['id']
    await cancel_active(dev_id)
    active_tasks[dev_id] = asyncio.create_task(switch_toggle(device, state))


# ---------------------------------------------------------------------------
# MQTT tasks
# ---------------------------------------------------------------------------

async def messages(client):
    async for topic, msg, retained in client.queue:
        try:
            topic_str = topic.decode()
            payload   = msg.decode().strip()

            if topic_str == CONFIG_TOPIC:
                apply_mqtt_config(payload)
                continue

            # Only the config topic is legitimately retained. A retained
            # command would be replayed on every reconnect and physically
            # move a shutter each time — drop it.
            if retained:
                continue

            # Command topics: {DEVICE_ID}/{shutter|switch}/{id}/{command}
            parts    = topic_str.split('/')
            if len(parts) < 4 or parts[0] != DEVICE_ID:
                continue
            dev_type = parts[1]
            device   = devices.get(int(parts[2]))
            if device is None:
                continue

            if dev_type == 'shutter' and device['type'] == 'shutter':
                await handle_shutter(device, parts[3], payload)
            elif dev_type == 'switch' and device['type'] == 'switch':
                await handle_switch(device, parts[3], payload)
        except Exception as e:
            print('Message error: {}'.format(e))


async def down(client):
    global outages
    while True:
        await client.down.wait()
        client.down.clear()
        wifi_led(False)
        outages += 1
        print('WiFi or broker is down.')


async def up(client):
    while True:
        await client.up.wait()
        client.up.clear()
        wifi_led(True)
        print('Connected to broker.')
        await client.publish(STATUS_TOPIC, 'online', retain=True, qos=1)
        await publish_discovery()
        await client.subscribe(CONFIG_TOPIC, 1)
        for dev_id, device in devices.items():
            if device['type'] == 'shutter':
                await client.subscribe('{}/shutter/{}/set_position'.format(DEVICE_ID, dev_id), 1)
                await client.subscribe('{}/shutter/{}/set'.format(DEVICE_ID, dev_id), 1)
            elif device['type'] == 'switch':
                await client.subscribe('{}/switch/{}/set'.format(DEVICE_ID, dev_id), 1)
        # Publish current states so HA never shows 'unknown' after a broker
        # reconnect, an HA restart, or a fresh device (state is not retained).
        for dev_id, device in devices.items():
            if device['type'] == 'shutter' and active_tasks.get(dev_id) is None:
                await publish_shutter_state(
                    device, 'closed' if device['position'] == 0 else 'open')
            elif device['type'] == 'switch':
                await publish_switch_state(
                    device, 'ON' if device['pin'].value() else 'OFF')


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

async def main(client):
    print('Device: {} ({})'.format(DEVICE_NAME, DEVICE_ID))

    device_list = load_config()

    # Retry until WiFi/broker come up — after a power outage the Pico boots
    # long before the router and broker do, and giving up here would leave
    # the relays dead until a manual power cycle. Hard-reset periodically in
    # case the WiFi chip is wedged in a state a fresh connect can't clear.
    attempts = 0
    while True:
        try:
            await client.connect()
            break
        except OSError:
            attempts += 1
            print('Connection failed (attempt {}) — retrying in 10 s.'.format(attempts))
            if attempts >= 30:
                machine.reset()
            await asyncio.sleep(10)

    for d in device_list:
        dev_id = d['id']
        if d['type'] == 'shutter':
            d['pin_up']   = Pin(d['relay_up'],   Pin.OUT, value=0)
            d['pin_down'] = Pin(d['relay_down'],  Pin.OUT, value=0)
        elif d['type'] == 'switch':
            d['pin'] = Pin(d['relay'], Pin.OUT, value=0)
        devices[dev_id]      = d
        active_tasks[dev_id] = None

    for task in (up, down, messages):
        asyncio.create_task(task(client))

    while True:
        await asyncio.sleep(30)
        await client.publish(STATUS_TOPIC, 'online', retain=True, qos=1)


# Will is retained so HA sees 'offline' even if it (re)subscribes after the
# board has already died; the periodic 'online' publish is retained likewise.
config['will']      = (STATUS_TOPIC, 'offline', True, 0)
config['keepalive'] = KEEPALIVE
config['queue_len'] = QUEUE_LEN
MQTTClient.DEBUG    = DEBUG
client = MQTTClient(config)

try:
    asyncio.run(main(client))
finally:
    client.close()
    blue_led(True)
    asyncio.new_event_loop()
