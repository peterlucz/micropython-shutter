# mqtt_local.py  Pico W configuration for mqtt_as.
from sys import implementation
from mqtt_as import config
from secrets import MQTT_USER, MQTT_PASSWORD, WIFI_SSID, WIFI_PASSWORD
from config import MQTT_SERVER, MQTT_OVERRIDE_FILE
from machine import Pin
import ujson

# Broker address/credentials can be overridden without a USB visit -- see
# apply_mqtt_override() in main.py, which writes MQTT_OVERRIDE_FILE on
# receipt of `{DEVICE_ID}/mqtt_config` and reboots to apply it (mqtt_as
# connects once at startup, there's no live re-point). WiFi credentials are
# deliberately NOT part of this -- updating WIFI_SSID/WIFI_PASSWORD this way
# would need the new WiFi already working before a message changing it could
# ever arrive, so that still requires a USB visit as before.
mqtt_server, mqtt_user, mqtt_password = MQTT_SERVER, MQTT_USER, MQTT_PASSWORD
try:
    with open(MQTT_OVERRIDE_FILE) as f:
        _override = ujson.load(f)
    mqtt_server   = _override.get('server',   mqtt_server)
    mqtt_user     = _override.get('user',     mqtt_user)
    mqtt_password = _override.get('password', mqtt_password)
except (OSError, ValueError):
    pass  # no override file yet, or it's corrupt -- fall back to the defaults above

config['server']   = mqtt_server
config['user']     = mqtt_user
config['password'] = mqtt_password
config['ssid']     = WIFI_SSID
config['wifi_pw']  = WIFI_PASSWORD

def _ledfunc(pin):
    def func(v):
        pin(v)
    return func

wifi_led = lambda _: None  # Pico W has no dedicated WiFi LED
_LED     = 'LED' if 'Pico W' in implementation._machine else 25
blue_led = _ledfunc(Pin(_LED, Pin.OUT, value=0))
