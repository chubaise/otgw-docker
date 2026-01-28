import socket
import time
import sys
import os
import logging
import re
import requests
from logging.handlers import TimedRotatingFileHandler
import paho.mqtt.client as mqtt

# --- НАСТРОЙКИ ---
OTGW_IP = os.getenv('OTGW_IP', '127.0.0.1')
OTGW_PORT = int(os.getenv('OTGW_PORT', 23))

MQTT_BROKER = os.getenv('MQTT_BROKER', 'localhost')
MQTT_PORT = int(os.getenv('MQTT_PORT', 1883))
MQTT_USER = os.getenv('MQTT_USER', None)
MQTT_PASS = os.getenv('MQTT_PASS', None)
TOPIC_ERROR = os.getenv('MQTT_TOPIC_ERROR', "otgw/error")

TG_TOKEN = os.getenv('TG_TOKEN', None)
TG_CHAT_ID = os.getenv('TG_CHAT_ID', None)

# Интервал отчета: 6 часов
REPORT_INTERVAL = 6 * 3600 

LOG_DIR = "/logs"
ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# СЛОВАРЬ ОШИБОК
ERROR_CODES = {
    "Error 01": "Ошибка четности (Помехи/контакт)",
    "Error 02": "Ошибка Stop-бита (Синхронизация)",
    "Error 03": "Переполнение буфера",
    "Error 04": "Неизвестный формат"
}

# Хранилище состояния
status = {
    "t_boiler": "---",
    "t_room": "---",
    "pressure": "---",
    "modulation": "---",
    "last_error": None
}

# --- ЛОГГЕР ---
logger = logging.getLogger("OTGW")
logger.setLevel(logging.INFO)
hourly_handler = TimedRotatingFileHandler(f"{LOG_DIR}/otgw_hourly.log", when="h", interval=1, backupCount=168)
hourly_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logger.addHandler(hourly_handler)
daily_handler = TimedRotatingFileHandler(f"{LOG_DIR}/otgw_daily.log", when="midnight", interval=1, backupCount=30)
daily_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logger.addHandler(daily_handler)

mqtt_connected = False
last_report_time = time.time()

# --- ФУНКЦИИ ---
def ot_float(hex_str):
    try:
        val = int(hex_str, 16)
        if val > 32767: val -= 65536
        return round(val / 256.0, 1)
    except: return 0.0

def parse_opentherm(line):
    if len(line) != 9 or line[0] not in ['T', 'B', 'R', 'A']: return
    try:
        msg_id = int(line[3:5], 16)
        data_hex = line[5:9]
        if msg_id == 25: status["t_boiler"] = ot_float(data_hex)
        elif msg_id == 24: status["t_room"] = ot_float(data_hex)
        elif msg_id == 18: status["pressure"] = ot_float(data_hex)
        elif msg_id == 17: status["modulation"] = ot_float(data_hex)
    except: pass

def send_telegram(message, silent=False):
    if TG_TOKEN and TG_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
            data = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "Markdown", "disable_notification": silent}
            requests.post(url, json=data, timeout=5)
        except: pass

def send_status_report():
    # Формируем строку ошибки для 2-й строки
    if status['last_error']:
        # Если была ошибка - показываем её
        err_desc = ERROR_CODES.get(status['last_error'], status['last_error'])
        error_line = f"⚠️ Ошибки: *{err_desc}*"
        # Сбрасываем ошибку после отчета (или оставить, если хотите помнить вечно)
        status['last_error'] = None 
    else:
        error_line = "✅ Ошибки: *Нет (Норма)*"

    msg = (
        f"📊 *Отчет о состоянии (6ч)*\n"
        f"{error_line}\n"                 # <--- 2-я строка как просили
        f"🌡 Комната: *{status['t_room']} °C*\n"
        f"🔥 Котел: *{status['t_boiler']} °C*\n"
        f"📈 Мощность: *{status['modulation']} %*\n"
        f"💧 Давление: *{status['pressure']} bar*"
    )
    send_telegram(msg, silent=True)

def on_connect(client, userdata, flags, rc):
    global mqtt_connected
    if rc == 0:
        print("Connected to MQTT!")
        mqtt_connected = True

# --- MAIN ---
def main():
    global last_report_time
    client = mqtt.Client()
    if MQTT_USER and MQTT_PASS:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
    except: print("MQTT Error")

    print("Starting...")
    send_telegram("🔄 Мониторинг перезапущен (v3.0 Final)")

    while True:
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10)
            s.connect((OTGW_IP, OTGW_PORT))
            print("Connected to OTGW!")
            
            buffer = ""
            while True:
                # Проверка времени для отчета (раз в 6 часов)
                if time.time() - last_report_time > REPORT_INTERVAL:
                    send_status_report()
                    last_report_time = time.time()

                data = s.recv(1024)
                if not data: break
                
                try:
                    text_chunk = data.decode('ascii', errors='ignore')
                    buffer += text_chunk
                    
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        clean_line = ansi_escape.sub('', line).strip()
                        if not clean_line or clean_line.startswith('['): continue
                        
                        logger.info(clean_line)
                        parse_opentherm(clean_line)

                        if "Error" in clean_line:
                            print(f"ERROR: {clean_line}")
                            status['last_error'] = clean_line
                            desc = ERROR_CODES.get(clean_line, "Неизвестная ошибка")
                            send_telegram(f"⚠️ *АВАРИЯ КОТЛА*\nКод: `{clean_line}`\n_{desc}_")
                            if mqtt_connected: client.publish(TOPIC_ERROR, clean_line)

                except: pass
        except socket.error:
            print("Socket lost")
        except: pass
        finally:
            if s: s.close()
            time.sleep(10)

if __name__ == "__main__":
    main()