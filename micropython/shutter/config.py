MQTT_SERVER      = '192.168.1.5'
# Status + config topics are derived from the MAC-based DEVICE_ID in main.py
# ('{DEVICE_ID}/status', '{DEVICE_ID}/config') so multiple boards can coexist.
DEVICES_FILE     = 'devices.json'
DISCOVERY_PREFIX = 'homeassistant'
KEEPALIVE        = 120
QUEUE_LEN        = 10  # HA cover groups send several commands at once; depth 1 drops them
DEBUG            = True
