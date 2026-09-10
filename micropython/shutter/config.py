MQTT_SERVER      = '10.30.0.100'
# Status + config topics are derived from the MAC-based DEVICE_ID in main.py
# ('{DEVICE_ID}/status', '{DEVICE_ID}/config') so multiple boards can coexist.
DEVICES_FILE     = 'devices.json'
DISCOVERY_FILE   = 'discovery.json'  # discovery topics currently retained on the broker
# Optional broker-override file, written by {DEVICE_ID}/mqtt_config (see
# main.py) to change the broker/credentials without a USB visit. Absent by
# default -- MQTT_SERVER/secrets.py's MQTT_USER/PASSWORD above apply until
# an override is pushed. See mqtt_local.py for how it's merged in at boot.
MQTT_OVERRIDE_FILE = 'mqtt_override.json'
DISCOVERY_PREFIX = 'homeassistant'
KEEPALIVE        = 120
QUEUE_LEN        = 10  # HA cover groups send several commands at once; depth 1 drops them
DEBUG            = True
