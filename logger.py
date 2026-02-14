import socket
import time
import sys
import os
import logging
import re
import json
import requests
from logging.handlers import TimedRotatingFileHandler
import paho.mqtt.client as mqtt

# --- SETTINGS ---
OTGW_IP = os.getenv('OTGW_IP', '127.0.0.1')
OTGW_PORT = int(os.getenv('OTGW_PORT', 23))

MQTT_BROKER = os.getenv('MQTT_BROKER', 'localhost')
MQTT_PORT = int(os.getenv('MQTT_PORT', 1883))
MQTT_USER = os.getenv('MQTT_USER', None)
MQTT_PASS = os.getenv('MQTT_PASS', None)

# MQTT Topics
TOPIC_ERROR = "otgw/error"          
TOPIC_ERROR_TEXT = "otgw/error_text" 
TOPIC_BOILER_STATE = "otgw/boiler_state"

TG_TOKEN = os.getenv('TG_TOKEN', None)
TG_CHAT_ID = os.getenv('TG_CHAT_ID', None)

REPORT_INTERVAL = 3600
WATCHDOG_TIMEOUT = 600
MIN_PRESSURE = 0.7
MAX_PRESSURE = 2.8

# Logger setup
LOG_DIR = "/logs"
if not os.path.exists(LOG_DIR): os.makedirs(LOG_DIR)
logger = logging.getLogger("OTGW")
logger.setLevel(logging.INFO)
hourly_handler = TimedRotatingFileHandler(f"{LOG_DIR}/otgw_hourly.log", when="h", interval=1, backupCount=168)
hourly_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logger.addHandler(hourly_handler)
daily_handler = TimedRotatingFileHandler(f"{LOG_DIR}/otgw_daily.log", when="midnight", interval=1, backupCount=30)
daily_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logger.addHandler(daily_handler)

# Regex patterns
ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
text_sensor_pattern = re.compile(r"'([^']+)'\s+new value.*?:\s*([\d\.]+)", re.IGNORECASE)
text_pressure_pattern = re.compile(r"Pressure.*?value.*?:\s*([\d\.]+)", re.IGNORECASE)
topic_pattern = re.compile(r'Topic:.*?/sensors/([^/\s]+)', re.IGNORECASE)
fault_pattern = re.compile(r'fault:\s*(\d)', re.IGNORECASE)
oem_code_pattern = re.compile(r'OEM fault code:\s*(\d+)', re.IGNORECASE)
verbose_pattern = re.compile(r'ID:\s*(\d+).*Response:\s*([0-9a-fA-F]{8})', re.IGNORECASE)

AMPERA_ERRORS = {
    17: "E9 - Heater/Relay power failure",
    1:  "E1 - Low pressure",
    2:  "E2 - Overheating / No flow",
    3:  "E3 - Critical overheating",
    4:  "E4 - Temp sensor failure",
    5:  "E5 - Outdoor/Boiler sensor failure"
}

status = {
    "t_boiler": "---", "t_return": "---", "t_dhw": "---", 
    "t_room": "---", "t_outdoor": "---",
    "pressure": "---", "modulation": "---",
    "is_boiler_fault": False, "last_fault_code": None,
    "low_pressure_alert": False, "errors_set": set(),
    "connection_alert": False, "emergency_mode": False
}

mqtt_connected = False
last_report_time = time.time() - REPORT_INTERVAL + 60
last_data_time = time.time() 
context_sensor_name = None
client = mqtt.Client()

def ot_float(hex_str):
    try:
        val = int(hex_str, 16)
        if val > 32767: val -= 65536
        return round(val / 256.0, 1)
    except: return 0.0

def send_telegram(message, silent=False):
    if TG_TOKEN and TG_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
            data = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML", "disable_notification": silent}
            requests.post(url, json=data, timeout=5)
        except: pass

def mqtt_publish_error(code, text):
    if mqtt_connected:
        client.publish(TOPIC_ERROR, code)       
        client.publish(TOPIC_ERROR_TEXT, text)  

def check_pressure(val):
    if val < MIN_PRESSURE and not status["low_pressure_alert"]:
        status["low_pressure_alert"] = True
        # Telegram with Emoji
        send_telegram(f"💧 <b>АВАРИЯ ДАВЛЕНИЯ!</b>\nНизкое давление: {val} bar")
        # MQTT Text (Clean)
        mqtt_publish_error("LOW_PRESSURE", f"Low pressure: {val} bar")

    elif val >= MIN_PRESSURE and status["low_pressure_alert"]:
        status["low_pressure_alert"] = False
        send_telegram(f"✅ <b>Давление в норме</b>: {val} bar")
        mqtt_publish_error("OK", "No errors")

def ping_watchdog():
    global last_data_time
    last_data_time = time.time()
    if status["connection_alert"]:
        status["connection_alert"] = False
        send_telegram("✅ <b>Связь со шлюзом восстановлена!</b>")
        mqtt_publish_error("OK", "Connection restored")

def check_watchdog():
    if time.time() - last_data_time > WATCHDOG_TIMEOUT and not status["connection_alert"]:
        status["connection_alert"] = True
        msg = "No connection to gateway (Timeout)"
        send_telegram(f"⚠️ <b>НЕТ СВЯЗИ СО ШЛЮЗОМ!</b>\n{msg}")
        mqtt_publish_error("CONNECTION_LOST", msg)

def update_status(key, val):
    try:
        val = float(val)
        updated = False
        key = key.lower().strip()
        
        if key == 'pressure':
             status['pressure'] = val
             check_pressure(val)
             updated = True
        elif key in ['t_boiler', 'boiler_temp', 'tr', 'heating temp', 'heating_temp']: 
             status['t_boiler'] = val
             updated = True
        elif key in ['t_dhw', 'dhw_temp', 'dhw', 'dhw temp']: 
             status['t_dhw'] = val
             updated = True
        elif key in ['modulation', 'mod', 'rel_mod', 'modulation level', 'modulation_level']: 
             status['modulation'] = val
             updated = True
        elif 'return' in key: 
             status['t_return'] = val
             updated = True
        elif 'outdoor' in key: 
             status['t_outdoor'] = val
             updated = True
        elif 'room' in key or 'indoor' in key: 
             status['t_room'] = val
             updated = True
             
        if updated: 
            print(f"DATA: {key} = {val}")
            ping_watchdog() 

    except: pass

def update_status_hex(msg_id, data_hex):
    try:
        val = ot_float(data_hex)
        ping_watchdog() 
        if msg_id == 25: update_status('t_boiler', val)
        elif msg_id == 28: update_status('t_return', val)
        elif msg_id == 26: update_status('t_dhw', val)
        elif msg_id == 24: update_status('t_room', val)
        elif msg_id == 27: update_status('t_outdoor', val)
        elif msg_id == 18: update_status('pressure', val)
        elif msg_id == 17: update_status('modulation', val)
        elif msg_id == 115 and val > 0: status["last_fault_code"] = int(val)
    except: pass

def check_boiler_fault(line):
    # 1. Check OEM code
    code_match = oem_code_pattern.search(line)
    if code_match: 
        status["last_fault_code"] = int(code_match.group(1))

    # 2. Check Fault bit
    match = fault_pattern.search(line)
    if match:
        ping_watchdog()
        fault_val = int(match.group(1))

        if fault_val == 1 and not status["is_boiler_fault"]:
            status["is_boiler_fault"] = True
            raw_code = status['last_fault_code']
            reason = AMPERA_ERRORS.get(raw_code, f"Code {raw_code}") if raw_code else "Unknown Error"
            
            # Telegram with Emoji
            send_telegram(f"🔥 <b>АВАРИЯ КОТЛА!</b>\nПричина: <b>{reason}</b>")
            
            mqtt_publish_error(f"FAULT_{raw_code or 'UNK'}", reason)
            if mqtt_connected: client.publish(TOPIC_BOILER_STATE, "error")

        elif fault_val == 0 and status["is_boiler_fault"]:
            status["is_boiler_fault"] = False
            status["last_fault_code"] = None
            send_telegram("✅ <b>Авария котла устранена</b>")
            mqtt_publish_error("OK", "No errors")
            if mqtt_connected: client.publish(TOPIC_BOILER_STATE, "ok")

def check_emergency_text(line):
    if "Emergency mode enabled" in line and not status["emergency_mode"]:
        status["emergency_mode"] = True
        # Telegram with Emoji
        send_telegram("🚨 <b>Аварийный режим (ограничение функционала)</b>")
        # MQTT Clean Text
        mqtt_publish_error("EMERGENCY", "Emergency Mode enabled")
        if mqtt_connected: client.publish(TOPIC_BOILER_STATE, "emergency")

    elif "Emergency mode disabled" in line and status["emergency_mode"]:
        status["emergency_mode"] = False
        send_telegram("✅ <b>Аварийный режим отключен</b>")
        mqtt_publish_error("OK", "No errors")
        if mqtt_connected: client.publish(TOPIC_BOILER_STATE, "ok")

def parse_line(line):
    global context_sensor_name
    check_emergency_text(line)

    topic_match = topic_pattern.search(line)
    if topic_match:
        context_sensor_name = topic_match.group(1)
        ping_watchdog()
        return

    if '{"value":' in line and context_sensor_name:
        try:
            data = json.loads(line[line.find('{'):line.rfind('}')+1])
            val = data.get('value')
            if val is not None:
                update_status(context_sensor_name, val)
                context_sensor_name = None
        except: pass

    sensor_match = text_sensor_pattern.search(line)
    if sensor_match:
        update_status(sensor_match.group(1), sensor_match.group(2))

    if "boiler status" in line.lower() or "fault" in line.lower() or "oem" in line.lower():
        check_boiler_fault(line)

    p_match = text_pressure_pattern.search(line)
    if p_match:
        try: update_status('pressure', float(p_match.group(1)))
        except: pass

    match = verbose_pattern.search(line)
    if match:
        try: update_status_hex(int(match.group(1)), match.group(2)[4:8])
        except: pass

def on_connect(c, userdata, flags, rc):
    global mqtt_connected
    if rc == 0:
        print("Connected to MQTT!")
        mqtt_connected = True
        c.publish(TOPIC_BOILER_STATE, "ok")
        c.publish(TOPIC_ERROR_TEXT, "No errors")

def main():
    global last_report_time
    if MQTT_USER and MQTT_PASS:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
    except: print("MQTT Error")

    print("Starting OTGW Monitor v3.25...")
    send_telegram("🔄 <b>Мониторинг v3.25</b>: Аварийный режим + MQTT")

    while True:
        s = None
        try:
            check_watchdog()
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(60)
            s.connect((OTGW_IP, OTGW_PORT))
            print("Connected to OTGW!")
            ping_watchdog()
            
            buffer = ""
            while True:
                check_watchdog()
                if time.time() - last_report_time > REPORT_INTERVAL:
                    last_report_time = time.time()

                try:
                    data = s.recv(1024)
                except socket.timeout:
                    break 

                if not data: break
                
                try:
                    text_chunk = data.decode('ascii', errors='ignore')
                    buffer += text_chunk
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        clean_line = ansi_escape.sub('', line).strip()
                        if not clean_line: continue
                        logger.info(clean_line)
                        parse_line(clean_line)
                except: pass

        except Exception as e:
            print(f"Connection lost: {e}. Retrying in 10s...")
            time.sleep(10)
        finally:
            if s: s.close()

if __name__ == "__main__":
    main()