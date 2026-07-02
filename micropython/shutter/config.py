MQTT_SERVER      = '192.168.1.5'
STATUS_TOPIC     = 'pico/status'
CONFIG_TOPIC     = 'pico/config'
DEVICES_FILE     = 'devices.json'
DISCOVERY_PREFIX = 'homeassistant'
KEEPALIVE        = 120
QUEUE_LEN        = 10  # HA cover groups send several commands at once; depth 1 drops them
DEBUG            = True
