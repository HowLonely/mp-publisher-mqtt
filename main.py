from pymodbus.client import ModbusSerialClient
import paho.mqtt.client as mqtt
import struct
import math
import time
from datetime import datetime
import json
import os
import threading
import logging

# ======================
# CONFIG RS485 / MODBUS
# ======================
SERIAL_PORT = "/dev/ttyUSB0"
BAUDRATE = 115200
PARITY = "E"
STOPBITS = 1
BYTESIZE = 8
TIMEOUT = 8
MODBUS_ID = 1

POLL_SECONDS = 5
START_ADDR = 47 # STEL / AVERAGE 1
COUNT = 10

RETRY_DELAY_SECONDS = 60
MAX_RETRIES = 1

# Publicacion
FORCE_PUBLISH_SECONDS = 60 # keepalive obligatorio
MIN_GAP_ON_CHANGE_SECONDS = 5 # anti-duplicado por cambios muy seguidos

# ============
# MQTT CONFIG
#=============
SERIAL_NUMBER = "3047889" # ajustar al numero de serie del equipo
TOPIC = f"controlworld/mp/casella/{SERIAL_NUMBER}/telemetry"

