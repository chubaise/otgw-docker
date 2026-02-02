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

# --- ЧИТАЕМ НАСТРОЙКИ ИЗ .ENV ---
OTGW_IP = os.getenv('OTGW_IP', '127.0.0.1')
OTGW_PORT = int(os.getenv('OTGW_PORT', 23))

MQTT_BROKER = os.getenv('MQTT_BROKER', 'localhost')
MQTT_PORT = int(os.getenv('MQTT_PORT', 1883))
MQTT_USER = os.getenv('MQTT_USER', None)
MQTT_PASS = os.getenv('MQTT_PASS', None)

# Вот переменная, которую вы искали:
TOPIC_ERROR = os.getenv('MQTT_TOPIC_ERROR', "otgw/error") 
TOPIC_BOILER_STATE = "otgw/boiler_state"

TG_TOKEN = os.getenv('TG_TOKEN', None)
TG_CHAT_ID = os.getenv('TG_CHAT_ID', None)

REPORT_INTERVAL = 3600
MIN_PRESSURE = 0.7
MAX_PRESSURE = 2.8

# Логгер
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

# Регулярные выражения
ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
text_sensor_pattern = re.compile(r"'([^']+)'\s+new value.*?:\s*([\d\.]+)", re.IGNORECASE)
text_pressure_pattern = re.compile(r"Pressure.*?value.*?:\s*([\d\.]+)", re.IGNORECASE)
topic_pattern = re.compile(r'Topic:.*?/sensors/([^/\s]+)', re.IGNORECASE)
fault_pattern = re.compile(r'fault:\s*(\d)', re.IGNORECASE)
oem_code_pattern = re.compile(r'OEM fault code:\s*(\d+)', re.IGNORECASE)
verbose_pattern = re.compile(r'ID:\s*(\d+).*Response:\s*([0-9a-fA-F]{8})', re.IGNORECASE)

AMPERA_ERRORS = {
    17: "E9 - Отсутствие питания ТЭН / Реле",
    1:  "E1 - Низкое давление теплоносителя",
    2:  "E2 - Перегрев / Нет протока",
    3:  "E3 - Аварийный перегрев",
    4:  "E4 - Обрыв датчика температуры",
    5:  "E5 - Обрыв датчика улицы/бойлера"
}

ERROR_CODES = {
    "Error 01": "Ошибка четности (Помехи)",
    "Error 02": "Ошибка Stop-бита",
    "Error 03": "Переполнение буфера",
    "Error 04": "Неизвестный формат"
}

status = {
    "t_boiler": "---", "t_return": "---", "t_dhw": "---", 
    "t_room": "---", "t_outdoor": "---",
    "pressure": "---", "modulation": "---",
    "is_boiler_fault": False, "last_fault_code": None,
    "low_pressure_alert": False, "errors_set": set()
}

mqtt_connected = False
last_report_time = time.time() - REPORT_INTERVAL + 60
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

def check_pressure(val):
    if val < MIN_PRESSURE and not status["low_pressure_alert"]:
        status["low_pressure_alert"] = True
        send_telegram(f"💧 <b>АВАРИЯ ДАВЛЕНИЯ!</b>\nТекущее: <b>{val} bar</b>")
        if mqtt_connected: client.publish(TOPIC_ERROR, "LOW_PRESSURE")
    elif val >= MIN_PRESSURE and status["low_pressure_alert"]:
        status["low_pressure_alert"] = False
        send_telegram(f"✅ <b>Давление в норме</b>: {val} bar")
        if mqtt_connected: client.publish(TOPIC_ERROR, "OK")

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
             
        if updated: print(f"✅ UPDATE: {key} = {val}")

    except: pass

def update_status_hex(msg_id, data_hex):
    try:
        val = ot_float(data_hex)
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
    match = fault_pattern.search(line)
    if match:
        fault_val = int(match.group(1))
        code_match = oem_code_pattern.search(line)
        if code_match: status["last_fault_code"] = int(code_match.group(1))

        if fault_val == 1 and not status["is_boiler_fault"]:
            status["is_boiler_fault"] = True
            raw_code = status['last_fault_code']
            reason = AMPERA_ERRORS.get(raw_code, f"Код {raw_code}") if raw_code else "Неизвестная"
            send_telegram(f"🔥 <b>АВАРИЯ КОТЛА!</b>\nПричина: <b>{reason}</b>")
            if mqtt_connected: 
                # Вот здесь мы отправляем ошибку в MQTT
                client.publish(TOPIC_ERROR, f"FAULT_{raw_code or 'UNK'}")
                client.publish(TOPIC_BOILER_STATE, "error")
        elif fault_val == 0 and status["is_boiler_fault"]:
            status["is_boiler_fault"] = False
            status["last_fault_code"] = None
            send_telegram("✅ <b>Авария котла устранена</b>")
            if mqtt_connected: 
                client.publish(TOPIC_ERROR, "OK")
                client.publish(TOPIC_BOILER_STATE, "ok")

def parse_line(line):
    global context_sensor_name
    
    # 1. Контекст MQTT
    topic_match = topic_pattern.search(line)
    if topic_match:
        context_sensor_name = topic_match.group(1)
        return

    # 2. Значение MQTT (используя контекст)
    if '{"value":' in line and context_sensor_name:
        try:
            data = json.loads(line[line.find('{'):line.rfind('}')+1])
            val = data.get('value')
            if val is not None:
                update_status(context_sensor_name, val)
                context_sensor_name = None
        except: pass

    # 3. Текстовые логи Laxilef
    sensor_match = text_sensor_pattern.search(line)
    if sensor_match:
        update_status(sensor_match.group(1), sensor_match.group(2))

    # 4. Проверка ошибок
    if "boiler status" in line.lower() or "fault" in line.lower():
        check_boiler_fault(line)

    # 5. Pressure fallback
    p_match = text_pressure_pattern.search(line)
    if p_match:
        try: update_status('pressure', float(p_match.group(1)))
        except: pass

    # 6. HEX
    match = verbose_pattern.search(line)
    if match:
        try: update_status_hex(int(match.group(1)), match.group(2)[4:8])
        except: pass

def send_status_report():
    if status['errors_set']:
        err_list = [f"• <code>{err}</code>: <i>{ERROR_CODES.get(err, 'Неизвестная')}</i>" for err in status['errors_set']]
        error_block = "⚠️ <b>Gateway Error:</b>\n" + "\n".join(err_list)
        status['errors_set'].clear()
    else:
        error_block = "✅ Связь: <b>Норма</b>"

    if status["is_boiler_fault"]:
        code = status['last_fault_code']
        desc = AMPERA_ERRORS.get(code, f"Код {code}") if code else "Нет данных"
        boiler_state = f"🔥 <b>АВАРИЯ: {desc}</b>"
    elif status["low_pressure_alert"]:
        boiler_state = f"💧 <b>НИЗКОЕ ДАВЛЕНИЕ ({status['pressure']} bar)</b>"
    else:
        boiler_state = "✅ Котел: <b>В работе</b>"

    msg = (
        f"📊 <b>Отчет (1ч)</b>\n"
        f"{boiler_state}\n"
        f"{error_block}\n\n"
        f"🏠 Комната: <b>{status['t_room']} °C</b>\n"
        f"🌲 Улица: <b>{status['t_outdoor']} °C</b>\n"
        f"🚿 ГВС: <b>{status['t_dhw']} °C</b>\n"
        f"🔥 Подача: <b>{status['t_boiler']} °C</b>\n"
        f"↩️ Обратка: <b>{status['t_return']} °C</b>\n"
        f"📈 Мощность: <b>{status['modulation']} %</b>\n"
        f"💧 Давление: <b>{status['pressure']} bar</b>"
    )
    send_telegram(msg, silent=True)

def on_connect(c, userdata, flags, rc):
    global mqtt_connected
    if rc == 0:
        print("Connected to MQTT!")
        mqtt_connected = True
        c.publish(TOPIC_BOILER_STATE, "ok")

def main():
    global last_report_time
    if MQTT_USER and MQTT_PASS:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
    except: print("MQTT Error")

    print("Starting OTGW Monitor v3.22 (Docker Edition)...")
    send_telegram("🔄 Мониторинг запущен (Docker)")

    while True:
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(60)
            s.connect((OTGW_IP, OTGW_PORT))
            print("Connected to OTGW!")
            
            buffer = ""
            while True:
                current_time = time.time()
                
                if current_time - last_report_time > REPORT_INTERVAL:
                    send_status_report()
                    last_report_time = current_time

                try:
                    data = s.recv(1024)
                except socket.timeout:
                    print("Timeout listening...")
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
                        if "Error" in clean_line and "fault" not in clean_line.lower():
                            status['errors_set'].add(clean_line)
                            # И здесь тоже отправляем сырые ошибки шлюза
                            if mqtt_connected: client.publish(TOPIC_ERROR, clean_line)
                except: pass

        except Exception as e:
            print(f"Connection lost: {e}. Retrying in 10s...")
            time.sleep(10)
        finally:
            if s: s.close()

if __name__ == "__main__":
    main()