# mqtt_local.py  Pico W configuration for mqtt_as.
from sys import implementation
from mqtt_as import config
from secrets import MQTT_USER, MQTT_PASSWORD, WIFI_SSID, WIFI_PASSWORD
from config import MQTT_SERVER
from machine import Pin

config['server']  = MQTT_SERVER
config['user']    = MQTT_USER
config['password'] = MQTT_PASSWORD
config['ssid']    = WIFI_SSID
config['wifi_pw'] = WIFI_PASSWORD

def _ledfunc(pin):
    def func(v):
        pin(v)
    return func

wifi_led = lambda _: None  # Pico W has no dedicated WiFi LED
_LED     = 'LED' if 'Pico W' in implementation._machine else 25
blue_led = _ledfunc(Pin(_LED, Pin.OUT, value=0))
