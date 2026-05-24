# range.py Test of asynchronous mqtt client with clean session False.
# (C) Copyright Peter Hinch 2017-2022.
# Released under the MIT licence.

# Now uses the event interface

# Public brokers https://github.com/mqtt/mqtt.github.io/wiki/public_brokers

# This demo is for wireless range tests. If OOR the red LED will light.
# In range the blue LED will pulse for each received message.
# Uses clean sessions to avoid backlog when OOR.

# red LED: ON == WiFi fail
# blue LED pulse == message received
# Publishes connection statistics.

from mqtt_as import MQTTClient
from mqtt_local import wifi_led, blue_led, config
import uasyncio as asyncio
from machine import Pin
import urequests
import ujson

TOPIC = 'shutter/a/state'  # For demo publication and last will use same topic
#TOPIC = 'shutter/pub'  # For demo publication and last will use same topic 
SHUTTERCFG = 'http://192.168.1.5/~peter/1.json'

outages = 0
number_of_messages = 0

async def pulse():  # This demo pulses blue LED each time a subscribed msg arrives.
    blue_led(True)
    await asyncio.sleep(1)
    blue_led(False)

async def relay_timer(relay_id, relay_time, topic_id, position):
    #print(relay["id"] % 2)
    if Pin(relay_id,Pin.OUT).value() == 0 and Pin(relay_id + 1 - 2*(relay_id % 2),Pin.OUT).value() == 0:
        Pin(relay_id,Pin.OUT).high()
        await asyncio.sleep_ms(relay_time)
        Pin(relay_id,Pin.OUT).low()
        shutter_data['shutter'][topic_id]['position'] = position
        if position == 0:
            new_state = "OFF"
        else:
            new_state = "ON"
        await client.publish('shutter/a/state/' + str(topic_id) , '{"state":"' + new_state + '","brightness":' + str(position) + '}', qos = 1)

async def messages(client):
    async for topic, msg, retained in client.queue:
        print("Start")
        try:
            msg_data = ujson.loads(msg.decode())
            topic_id = int(topic.decode()[-1])
            shutter = shutter_data['shutter'][topic_id]
            current_position = shutter['position']
            if "brightness" in msg_data:
                new_position = msg_data["brightness"]
            else:
                if msg_data["state"] == 'ON':
                    new_position = 100
                else:
                    new_position = 0

            print(f'Topic ID: "{topic_id}" Current position: "{current_position}" New position: "{new_position}"')

            if new_position > current_position:
                full_time = shutter['time_up']
                relay_id = shutter['up']
                relay_time = int(full_time * (new_position - current_position) * 10)
                action = True
                print(f'Move up, Relay ID: "{relay_id}", Relay time: "{relay_time}" ms')
            elif new_position < current_position:
                full_time = shutter['time_down']
                relay_id = shutter['down']
                relay_time = int(full_time * (current_position - new_position) * 10)
                action = True
                print(f'Move down, Relay ID: "{relay_id}", Relay time: "{relay_time}" ms')
            else:
                action = False
                print("No action needed!")

            if action:
                asyncio.create_task(relay_timer(relay_id, relay_time, topic_id, new_position))

        #asyncio.create_task(pulse())
        except:
            print("Message is not the right JSON data")
        

async def down(client):
    global outages
    while True:
        await client.down.wait()  # Pause until connectivity changes
        client.down.clear()
        wifi_led(False)
        outages += 1
        print('WiFi or broker is down.')

async def up(client):
    while True:
        await client.up.wait()
        client.up.clear()
        wifi_led(True)
        print('We are connected to broker.')
        await client.subscribe('shutter/a/set/0', 1)
        await client.subscribe('shutter/a/set/1', 1)
        await client.subscribe('shutter/a/set/2', 1)
        await client.subscribe('shutter/a/set/3', 1)

async def main(client):
    global shutter_data
    try:
        await client.connect()
    except OSError:
        print('Connection failed.')
        return

    response = urequests.get(SHUTTERCFG)
    shutter_data = ujson.loads(response.text)
    print(f"data is: {shutter_data}, url is: {SHUTTERCFG}")
    for ii in range (14, 21):
        Pin(ii,Pin.OUT).low()

    for task in (up, down, messages):
        asyncio.create_task(task(client))
    n = 0
    while True:
        await asyncio.sleep(5)
        # print('publish', n)
        # If WiFi is down the following will pause for the duration.
        await client.publish(TOPIC, '{} repubs: {} outages: {}'.format(n, client.REPUB_COUNT, outages), qos = 1)
        n += 1

# Define configuration
config['will'] = (TOPIC, 'Goodbye cruel world!', False, 0)
config['keepalive'] = 120
config["queue_len"] = 1  # Use event interface with default queue

# Set up client. Enable optional debug statements.
MQTTClient.DEBUG = True
client = MQTTClient(config)

try:
    asyncio.run(main(client))
finally:  # Prevent LmacRxBlk:1 errors.
    client.close()
    blue_led(True)
    asyncio.new_event_loop()
