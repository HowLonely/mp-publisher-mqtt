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

# =========================
# CONFIG RS485 / MODBUS
# =========================
SERIAL_PORT = "/dev/ttyUSB0"
BAUDRATE = 115200
PARITY = "E"
STOPBITS = 1
BYTESIZE = 8
TIMEOUT = 8
MODBUS_ID = 1

POLL_SECONDS = 5
START_ADDR = 47   # STEL / Average 1
COUNT = 10

RETRY_DELAY_SECONDS = 0.5
MAX_RETRIES = 1

# Publicación
FORCE_PUBLISH_SECONDS = 60     # keepalive obligatorio
MIN_GAP_ON_CHANGE_SECONDS = 5  # anti-duplicado por cambios muy seguidos

# =========================
# MQTT CONFIG
# =========================
SERIAL_NUMBER = "0210102835"  # <-- AJUSTA AL N° DE SERIE REAL DEL EQUIPO
TOPIC = f"controlworld/mp/trolex/{SERIAL_NUMBER}/telemetry"
BROKER = "167.172.254.167"
PORT = 1883
MQTT_USER = "esp32"
MQTT_PASS = "esp32_iot"

# =========================
# BACKUP CONFIG
# =========================
BACKUP_DIR = os.path.expanduser("~/trolex_pending")
BACKUP_FILE = os.path.join(BACKUP_DIR, "pendientes.jsonl")
MAX_BACKUP_SIZE_MB = 100
os.makedirs(BACKUP_DIR, exist_ok=True)

# =========================
# LOGGING
# =========================
logging.basicConfig(
    filename='trolex_stel_mqtt.log',
    filemode='a',
    format='>>> %(asctime)s >> %(levelname)s:\t%(message)s',
    level=logging.INFO
)
logging.getLogger().addHandler(logging.StreamHandler())

# =========================
# CLIENTS
# =========================
modbus_client = ModbusSerialClient(
    port=SERIAL_PORT,
    baudrate=BAUDRATE,
    parity=PARITY,
    stopbits=STOPBITS,
    bytesize=BYTESIZE,
    timeout=TIMEOUT
)

mqtt_client = mqtt.Client()
mqtt_client.username_pw_set(MQTT_USER, password=MQTT_PASS)
mqtt_connected = threading.Event()

# =========================
# HELPERS
# =========================
def f32_abcd(r1, r2):
    try:
        v = struct.unpack(">f", struct.pack(">HH", r1, r2))[0]
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, 6)
    except Exception:
        return None

def values_are_complete(values):
    return all(v is not None for v in values.values())

def values_non_negative(values):
    # Filtro máximo desactivado:
    # solo validamos que no sean negativos.
    for k, v in values.items():
        if v is None:
            return False
        if v < 0:
            logging.warning(f"Valor negativo detectado: {k}={v}")
            return False
    return True

def values_monotonic(values):
    seq = [values["pm1"], values["pm25"], values["pm4_25"], values["pm10"], values["tsp"]]
    return seq[0] <= seq[1] <= seq[2] <= seq[3] <= seq[4]

def looks_suspicious(values):
    if not values_are_complete(values):
        return True, "algún valor es None"
    if not values_non_negative(values):
        return True, "valor negativo detectado"
    if not values_monotonic(values):
        return True, "secuencia no monotónica"
    return False, None

def read_once():
    try:
        rr = modbus_client.read_input_registers(address=START_ADDR, count=COUNT, slave=MODBUS_ID)

        if rr.isError():
            return None, "ERROR_MODBUS"

        regs = rr.registers if hasattr(rr, "registers") and rr.registers else []
        if len(regs) != 10:
            return {"raw": regs}, f"LECTURA_INCOMPLETA_{len(regs)}"

        values = {
            "pm1": f32_abcd(regs[0], regs[1]),
            "pm25": f32_abcd(regs[2], regs[3]),
            "pm4_25": f32_abcd(regs[4], regs[5]),
            "pm10": f32_abcd(regs[6], regs[7]),
            "tsp": f32_abcd(regs[8], regs[9]),
        }

        suspicious, reason = looks_suspicious(values)
        if suspicious:
            return {"raw": regs, "values": values}, f"LECTURA_SOSPECHOSA_{reason}"

        return {"raw": regs, "values": values}, None

    except Exception as e:
        return None, f"EXCEPTION_{e}"

def read_with_retry():
    last_result = None
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        result, error = read_once()
        if error is None:
            return result, None, attempt
        last_result, last_error = result, error
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_SECONDS)
    return last_result, last_error, MAX_RETRIES

def build_legacy_payload(values):
    timestamp_ms = int(datetime.now().timestamp() * 1000)
    return {
        "pm1": {"value": values["pm1"], "timestamp": timestamp_ms},
        "pm25": {"value": values["pm25"], "timestamp": timestamp_ms},
        "pm4": {"value": values["pm4_25"], "timestamp": timestamp_ms},         # compat Node-RED
        "pm10": {"value": values["pm10"], "timestamp": timestamp_ms},
        "tsp_average": {"value": values["tsp"], "timestamp": timestamp_ms},     # compat Node-RED
    }

# =========================
# MQTT CALLBACKS
# =========================
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logging.info("✅ Conectado a MQTT")
        mqtt_connected.set()
    else:
        logging.warning(f"❌ Fallo al conectar MQTT. Código {rc}")
        mqtt_connected.clear()

def on_disconnect(client, userdata, rc):
    logging.warning("⚠️ MQTT desconectado")
    mqtt_connected.clear()

def mqtt_loop():
    while True:
        try:
            mqtt_client.connect(BROKER, PORT, 60)
            mqtt_client.loop_forever()
        except Exception as e:
            logging.warning(f"🔌 Reintentando conexión MQTT: {e}")
            time.sleep(10)

# =========================
# BACKUP
# =========================
def save_pending(payload):
    try:
        with open(BACKUP_FILE, "a") as f:
            f.write(json.dumps(payload) + "\n")
        logging.info("💾 Guardado localmente")
    except Exception as e:
        logging.warning(f"Error guardando backup local: {e}")

def resend_pending():
    if not os.path.exists(BACKUP_FILE):
        return

    lines_to_keep = []
    try:
        with open(BACKUP_FILE, "r") as f:
            for line in f:
                try:
                    data = json.loads(line)
                    result = mqtt_client.publish(TOPIC, json.dumps(data))
                    if result.rc != 0:
                        lines_to_keep.append(line)
                    time.sleep(0.1)
                except Exception:
                    lines_to_keep.append(line)

        with open(BACKUP_FILE, "w") as f:
            f.writelines(lines_to_keep)

        cleanup_backup()
    except Exception as e:
        logging.warning(f"Error reenviando pendientes: {e}")

def cleanup_backup():
    if os.path.exists(BACKUP_FILE):
        size_mb = os.path.getsize(BACKUP_FILE) / (1024 * 1024)
        if size_mb > MAX_BACKUP_SIZE_MB:
            logging.warning("⚠️ Backup superó tamaño límite, eliminado")
            os.remove(BACKUP_FILE)

def publish_payload(payload):
    if mqtt_connected.is_set():
        try:
            result_pub = mqtt_client.publish(TOPIC, json.dumps(payload))
            if result_pub.rc == 0:
                resend_pending()
                logging.info("✅ Dato publicado por MQTT")
                return True
            else:
                logging.warning("🚫 Error al publicar (rc != 0). Guardando local.")
                save_pending(payload)
                return False
        except Exception as e:
            logging.warning(f"🚫 Excepción al publicar ({e}). Guardando local.")
            save_pending(payload)
            return False
    else:
        logging.warning("🚫 MQTT desconectado. Guardando local.")
        save_pending(payload)
        return False

# =========================
# MAIN
# =========================
def main():
    if not modbus_client.connect():
        logging.error("❌ No se pudo conectar al RS485")
        return

    mqtt_client.on_connect = on_connect
    mqtt_client.on_disconnect = on_disconnect
    threading.Thread(target=mqtt_loop, daemon=True).start()

    logging.info("✅ Conectado al RS485")
    logging.info(f"📡 Leyendo STEL cada {POLL_SECONDS}s")
    logging.info(f"📤 Keepalive obligatorio cada {FORCE_PUBLISH_SECONDS}s")
    logging.info(f"🧭 Topic MQTT: {TOPIC}")
    logging.info("🛡️ Filtro máximo DESACTIVADO (solo no-negativo + monotonicidad)")

    last_sent_values = None
    last_publish_time = 0.0
    next_force_publish_time = time.time() + FORCE_PUBLISH_SECONDS

    while True:
        result, error, retries_used = read_with_retry()

        if retries_used > 0:
            logging.info(f"↻ Reintentos usados: {retries_used}")

        now_ts = time.time()

        if error:
            if result and "raw" in result:
                logging.warning(f"RAW: {result['raw']}")
            if result and "values" in result:
                logging.warning(f"VALUES: {result['values']}")
            logging.warning(f"⚠️ {error}")
            time.sleep(POLL_SECONDS)
            continue

        raw = result["raw"]
        values = result["values"]

        logging.info(f"RAW: {raw}")
        logging.info(f"STEL leído: {values}")

        changed = (last_sent_values is None or values != last_sent_values)
        force_due = (now_ts >= next_force_publish_time)
        min_gap_ok = (last_publish_time == 0.0 or (now_ts - last_publish_time) >= MIN_GAP_ON_CHANGE_SECONDS)

        publish_reason = None
        if changed and min_gap_ok:
            publish_reason = "cambio"
        elif force_due:
            publish_reason = f"keepalive_{FORCE_PUBLISH_SECONDS}s"

        if publish_reason:
            payload = build_legacy_payload(values)
            logging.info(f"📤 Publicando ({publish_reason}): {payload}")

            ok = publish_payload(payload)
            if ok:
                last_sent_values = values.copy()
                last_publish_time = now_ts
                next_force_publish_time = now_ts + FORCE_PUBLISH_SECONDS
        else:
            logging.info("⏸️ Sin publish en este ciclo (sin cambio o min_gap activo)")

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    logging.info("\n\n########################################\n##  Trolex AIR XD - STEL -> MQTT      ##\n########################################\n")
    main()
raspberrypi@pi:~ $ nano trolex_rs485.py
raspberrypi@pi:~ $ ^C
raspberrypi@pi:~ $ cat trolex_rs485.py
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

# =========================
# CONFIG RS485 / MODBUS
# =========================
SERIAL_PORT = "/dev/ttyUSB0"
BAUDRATE = 115200
PARITY = "E"
STOPBITS = 1
BYTESIZE = 8
TIMEOUT = 8
MODBUS_ID = 1

POLL_SECONDS = 5
START_ADDR = 47   # STEL / Average 1
COUNT = 10

RETRY_DELAY_SECONDS = 0.5
MAX_RETRIES = 1

# Publicación
FORCE_PUBLISH_SECONDS = 60     # keepalive obligatorio
MIN_GAP_ON_CHANGE_SECONDS = 5  # anti-duplicado por cambios muy seguidos

# =========================
# MQTT CONFIG
# =========================
SERIAL_NUMBER = "0210102835"  # <-- AJUSTA AL N° DE SERIE REAL DEL EQUIPO
TOPIC = f"controlworld/mp/trolex/{SERIAL_NUMBER}/telemetry"
BROKER = "167.172.254.167"
PORT = 1883
MQTT_USER = "esp32"
MQTT_PASS = "esp32_iot"

# =========================
# BACKUP CONFIG
# =========================
BACKUP_DIR = os.path.expanduser("~/trolex_pending")
BACKUP_FILE = os.path.join(BACKUP_DIR, "pendientes.jsonl")
MAX_BACKUP_SIZE_MB = 100
os.makedirs(BACKUP_DIR, exist_ok=True)

# =========================
# LOGGING
# =========================
logging.basicConfig(
    filename='trolex_stel_mqtt.log',
    filemode='a',
    format='>>> %(asctime)s >> %(levelname)s:\t%(message)s',
    level=logging.INFO
)
logging.getLogger().addHandler(logging.StreamHandler())

# =========================
# CLIENTS
# =========================
modbus_client = ModbusSerialClient(
    port=SERIAL_PORT,
    baudrate=BAUDRATE,
    parity=PARITY,
    stopbits=STOPBITS,
    bytesize=BYTESIZE,
    timeout=TIMEOUT
)

mqtt_client = mqtt.Client()
mqtt_client.username_pw_set(MQTT_USER, password=MQTT_PASS)
mqtt_connected = threading.Event()

# =========================
# HELPERS
# =========================
def f32_abcd(r1, r2):
    try:
        v = struct.unpack(">f", struct.pack(">HH", r1, r2))[0]
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, 6)
    except Exception:
        return None

def values_are_complete(values):
    return all(v is not None for v in values.values())

def values_non_negative(values):
    # Filtro máximo desactivado:
    # solo validamos que no sean negativos.
    for k, v in values.items():
        if v is None:
            return False
        if v < 0:
            logging.warning(f"Valor negativo detectado: {k}={v}")
            return False
    return True

def values_monotonic(values):
    seq = [values["pm1"], values["pm25"], values["pm4_25"], values["pm10"], values["tsp"]]
    return seq[0] <= seq[1] <= seq[2] <= seq[3] <= seq[4]

def looks_suspicious(values):
    if not values_are_complete(values):
        return True, "algún valor es None"
    if not values_non_negative(values):
        return True, "valor negativo detectado"
    if not values_monotonic(values):
        return True, "secuencia no monotónica"
    return False, None

def read_once():
    try:
        rr = modbus_client.read_input_registers(address=START_ADDR, count=COUNT, slave=MODBUS_ID)

        if rr.isError():
            return None, "ERROR_MODBUS"

        regs = rr.registers if hasattr(rr, "registers") and rr.registers else []
        if len(regs) != 10:
            return {"raw": regs}, f"LECTURA_INCOMPLETA_{len(regs)}"

        values = {
            "pm1": f32_abcd(regs[0], regs[1]),
            "pm25": f32_abcd(regs[2], regs[3]),
            "pm4_25": f32_abcd(regs[4], regs[5]),
            "pm10": f32_abcd(regs[6], regs[7]),
            "tsp": f32_abcd(regs[8], regs[9]),
        }

        suspicious, reason = looks_suspicious(values)
        if suspicious:
            return {"raw": regs, "values": values}, f"LECTURA_SOSPECHOSA_{reason}"

        return {"raw": regs, "values": values}, None

    except Exception as e:
        return None, f"EXCEPTION_{e}"

def read_with_retry():
    last_result = None
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        result, error = read_once()
        if error is None:
            return result, None, attempt
        last_result, last_error = result, error
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_SECONDS)
    return last_result, last_error, MAX_RETRIES

def build_legacy_payload(values):
    timestamp_ms = int(datetime.now().timestamp() * 1000)
    return {
        "pm1": {"value": values["pm1"], "timestamp": timestamp_ms},
        "pm25": {"value": values["pm25"], "timestamp": timestamp_ms},
        "pm4": {"value": values["pm4_25"], "timestamp": timestamp_ms},         # compat Node-RED
        "pm10": {"value": values["pm10"], "timestamp": timestamp_ms},
        "tsp_average": {"value": values["tsp"], "timestamp": timestamp_ms},     # compat Node-RED
    }

# =========================
# MQTT CALLBACKS
# =========================
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logging.info("✅ Conectado a MQTT")
        mqtt_connected.set()
    else:
        logging.warning(f"❌ Fallo al conectar MQTT. Código {rc}")
        mqtt_connected.clear()

def on_disconnect(client, userdata, rc):
    logging.warning("⚠️ MQTT desconectado")
    mqtt_connected.clear()

def mqtt_loop():
    while True:
        try:
            mqtt_client.connect(BROKER, PORT, 60)
            mqtt_client.loop_forever()
        except Exception as e:
            logging.warning(f"🔌 Reintentando conexión MQTT: {e}")
            time.sleep(10)

# =========================
# BACKUP
# =========================
def save_pending(payload):
    try:
        with open(BACKUP_FILE, "a") as f:
            f.write(json.dumps(payload) + "\n")
        logging.info("💾 Guardado localmente")
    except Exception as e:
        logging.warning(f"Error guardando backup local: {e}")

def resend_pending():
    if not os.path.exists(BACKUP_FILE):
        return

    lines_to_keep = []
    try:
        with open(BACKUP_FILE, "r") as f:
            for line in f:
                try:
                    data = json.loads(line)
                    result = mqtt_client.publish(TOPIC, json.dumps(data))
                    if result.rc != 0:
                        lines_to_keep.append(line)
                    time.sleep(0.1)
                except Exception:
                    lines_to_keep.append(line)

        with open(BACKUP_FILE, "w") as f:
            f.writelines(lines_to_keep)

        cleanup_backup()
    except Exception as e:
        logging.warning(f"Error reenviando pendientes: {e}")

def cleanup_backup():
    if os.path.exists(BACKUP_FILE):
        size_mb = os.path.getsize(BACKUP_FILE) / (1024 * 1024)
        if size_mb > MAX_BACKUP_SIZE_MB:
            logging.warning("⚠️ Backup superó tamaño límite, eliminado")
            os.remove(BACKUP_FILE)

def publish_payload(payload):
    if mqtt_connected.is_set():
        try:
            result_pub = mqtt_client.publish(TOPIC, json.dumps(payload))
            if result_pub.rc == 0:
                resend_pending()
                logging.info("✅ Dato publicado por MQTT")
                return True
            else:
                logging.warning("🚫 Error al publicar (rc != 0). Guardando local.")
                save_pending(payload)
                return False
        except Exception as e:
            logging.warning(f"🚫 Excepción al publicar ({e}). Guardando local.")
            save_pending(payload)
            return False
    else:
        logging.warning("🚫 MQTT desconectado. Guardando local.")
        save_pending(payload)
        return False

# =========================
# MAIN
# =========================
def main():
    if not modbus_client.connect():
        logging.error("❌ No se pudo conectar al RS485")
        return

    mqtt_client.on_connect = on_connect
    mqtt_client.on_disconnect = on_disconnect
    threading.Thread(target=mqtt_loop, daemon=True).start()

    logging.info("✅ Conectado al RS485")
    logging.info(f"📡 Leyendo STEL cada {POLL_SECONDS}s")
    logging.info(f"📤 Keepalive obligatorio cada {FORCE_PUBLISH_SECONDS}s")
    logging.info(f"🧭 Topic MQTT: {TOPIC}")
    logging.info("🛡️ Filtro máximo DESACTIVADO (solo no-negativo + monotonicidad)")

    last_sent_values = None
    last_publish_time = 0.0
    next_force_publish_time = time.time() + FORCE_PUBLISH_SECONDS

    while True:
        result, error, retries_used = read_with_retry()

        if retries_used > 0:
            logging.info(f"↻ Reintentos usados: {retries_used}")

        now_ts = time.time()

        if error:
            if result and "raw" in result:
                logging.warning(f"RAW: {result['raw']}")
            if result and "values" in result:
                logging.warning(f"VALUES: {result['values']}")
            logging.warning(f"⚠️ {error}")
            time.sleep(POLL_SECONDS)
            continue

        raw = result["raw"]
        values = result["values"]

        logging.info(f"RAW: {raw}")
        logging.info(f"STEL leído: {values}")

        changed = (last_sent_values is None or values != last_sent_values)
        force_due = (now_ts >= next_force_publish_time)
        min_gap_ok = (last_publish_time == 0.0 or (now_ts - last_publish_time) >= MIN_GAP_ON_CHANGE_SECONDS)

        publish_reason = None
        if changed and min_gap_ok:
            publish_reason = "cambio"
        elif force_due:
            publish_reason = f"keepalive_{FORCE_PUBLISH_SECONDS}s"

        if publish_reason:
            payload = build_legacy_payload(values)
            logging.info(f"📤 Publicando ({publish_reason}): {payload}")

            ok = publish_payload(payload)
            if ok:
                last_sent_values = values.copy()
                last_publish_time = now_ts
                next_force_publish_time = now_ts + FORCE_PUBLISH_SECONDS
        else:
            logging.info("⏸️ Sin publish en este ciclo (sin cambio o min_gap activo)")

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    logging.info("\n\n########################################\n##  Trolex AIR XD - STEL -> MQTT      ##\n########################################\n")
    main()
