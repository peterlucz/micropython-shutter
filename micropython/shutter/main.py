from mqtt_as import MQTTClient
from mqtt_local import wifi_led, blue_led, config
from config import (STATUS_TOPIC, CONFIG_TOPIC, DEVICES_FILE,
                    DISCOVERY_PREFIX, KEEPALIVE, QUEUE_LEN, DEBUG)
import uasyncio as asyncio
from machine import Pin
import network
import ujson
import utime
import gc

# Set in main() from the WiFi MAC address — e.g. 'pico_relay_ab12cd'.
DEVICE_ID   = None
DEVICE_NAME = None

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
    with open(DEVICES_FILE, 'w') as f:
        ujson.dump(new_data, f)
    print('Config updated on disk — reboot to apply new device layout.')


# ---------------------------------------------------------------------------
# State publishing
# ---------------------------------------------------------------------------

async def publish_shutter_state(device, state):
    payload = ujson.dumps({'state': state, 'position': device['position']})
    await client.publish('shutter/{}/state'.format(device['id']), payload, qos=1)


async def publish_switch_state(device, state):
    await client.publish('switch/{}/state'.format(device['id']), state, qos=1)


async def publish_discovery():
    dev_info = {'ids': [DEVICE_ID], 'name': DEVICE_NAME,
                'mdl': 'Waveshare Pico-Relay-B', 'mf': 'Waveshare'}
    for dev_id, device in devices.items():
        if device['type'] == 'shutter':
            topic = '{}/cover/{}_{}/config'.format(DISCOVERY_PREFIX, DEVICE_ID, dev_id)
            payload = ujson.dumps({
                'name':        'Shutter {}'.format(dev_id),
                'uniq_id':     '{}_{}_shutter_{}'.format(DEVICE_ID, DEVICE_NAME.replace(' ', '_'), dev_id),
                'cmd_t':       'shutter/{}/set'.format(dev_id),
                'set_pos_t':   'shutter/{}/set_position'.format(dev_id),
                'stat_t':      'shutter/{}/state'.format(dev_id),
                'val_tpl':     '{{ value_json.state }}',
                'pos_t':       'shutter/{}/state'.format(dev_id),
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
                'cmd_t':       'switch/{}/set'.format(dev_id),
                'stat_t':      'switch/{}/state'.format(dev_id),
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
        move_time  = int(device['time_up'] * (target - start) / 100)
        direction  = 1
        move_state = 'opening'
    else:
        pin_active = device['pin_down']
        pin_idle   = device['pin_up']
        move_time  = int(device['time_down'] * (start - target) / 100)
        direction  = -1
        move_state = 'closing'

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
        end_state = 'open' if target == 100 else 'closed' if target == 0 else 'open'
        await publish_shutter_state(device, end_state)
    except asyncio.CancelledError:
        pin_active.low()
        elapsed = utime.ticks_diff(utime.ticks_ms(), t0)
        delta = round(elapsed / move_time * abs(target - start))
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
        except:
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

            parts    = topic_str.split('/')
            if len(parts) < 3:
                continue
            dev_type = parts[0]
            device   = devices.get(int(parts[1]))
            if device is None:
                continue

            if dev_type == 'shutter' and device['type'] == 'shutter':
                await handle_shutter(device, parts[2], payload)
            elif dev_type == 'switch' and device['type'] == 'switch':
                await handle_switch(device, parts[2], payload)
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
                await client.subscribe('shutter/{}/set_position'.format(dev_id), 1)
                await client.subscribe('shutter/{}/set'.format(dev_id), 1)
            elif device['type'] == 'switch':
                await client.subscribe('switch/{}/set'.format(dev_id), 1)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

async def main(client):
    global DEVICE_ID, DEVICE_NAME
    _mac    = network.WLAN(network.STA_IF).config('mac')
    _suffix = ''.join('{:02x}'.format(b) for b in _mac[-3:])
    DEVICE_ID   = 'pico_relay_{}'.format(_suffix)
    DEVICE_NAME = 'Pico Relay {}'.format(_suffix.upper())
    print('Device: {} ({})'.format(DEVICE_NAME, DEVICE_ID))

    device_list = load_config()

    try:
        await client.connect()
    except OSError:
        print('Connection failed.')
        return

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


config['will']      = (STATUS_TOPIC, 'offline', False, 0)
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
