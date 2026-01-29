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

REPORT_INTERVAL = 3600  # Отчет раз в час
POLL_INTERVAL = 60      # Опрос котла раз в минуту

LOG_DIR = "/logs"
ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
verbose_pattern = re.compile(r'ID:\s*(\d+).*Response:\s*([0-9a-fA-F]{8})', re.IGNORECASE)

ERROR_CODES = {
    "Error 01": "Ошибка четности (Помехи)",
    "Error 02": "Ошибка Stop-бита",
    "Error 03": "Переполнение буфера",
    "Error 04": "Неизвестный формат"
}

status = {
    "t_boiler": "---",
    "t_dhw": "---",
    "pressure": "---",
    "modulation": "---",
    "errors_set": set()
}

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
last_poll_time = 0
client = mqtt.Client()

def ot_float(hex_str):
    try:
        val = int(hex_str, 16)
        if val > 32767: val -= 65536
        return round(val / 256.0, 1)
    except: return 0.0

def update_status_hex(msg_id, data_hex):
    try:
        val = ot_float(data_hex)
        if msg_id == 25: status["t_boiler"] = val
        elif msg_id == 26: status["t_dhw"] = val
        elif msg_id == 18: status["pressure"] = val
        elif msg_id == 17: status["modulation"] = val
    except: pass

def parse_line(line):
    # 1. Поиск JSON (на всякий случай)
    if '{' in line and '}' in line:
        try:
            # Тут можно добавить логику JSON, если понадобится
            pass
        except: pass

    # 2. Поиск HEX (Стандарт)
    if len(line) == 9 and line[0] in ['T', 'B', 'R', 'A']:
        try:
            msg_id = int(line[3:5], 16)
            data_hex = line[5:9]
            update_status_hex(msg_id, data_hex)
        except: pass
        return

    # 3. Поиск Verbose (Ваш случай)
    match = verbose_pattern.search(line)
    if match:
        try:
            msg_id = int(match.group(1))
            full_response = match.group(2)
            data_hex = full_response[4:8]
            update_status_hex(msg_id, data_hex)
        except: pass

def send_telegram(message, silent=False):
    if TG_TOKEN and TG_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
            data = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "Markdown", "disable_notification": silent}
            requests.post(url, json=data, timeout=5)
        except: pass

def send_status_report():
    if status['errors_set']:
        err_list = [f"• `{err}`: _{ERROR_CODES.get(err, 'Неизвестная')}_" for err in status['errors_set']]
        error_block = "⚠️ *Зафиксированы ошибки:*\n" + "\n".join(err_list)
        status['errors_set'].clear()
    else:
        error_block = "✅ Ошибки: *Нет (Норма)*"

    msg = (
        f"📊 *Отчет (1ч)*\n"
        f"{error_block}\n\n"
        f"🚿 ГВС: *{status['t_dhw']} °C*\n"
        f"🔥 Котел: *{status['t_boiler']} °C*\n"
        f"📈 Мощность: *{status['modulation']} %*\n"
        f"💧 Давление: *{status['pressure']} bar*"
    )
    send_telegram(msg, silent=True)

def on_connect(c, userdata, flags, rc):
    global mqtt_connected
    if rc == 0:
        print("Connected to MQTT!")
        mqtt_connected = True

def main():
    global last_report_time, last_poll_time
    if MQTT_USER and MQTT_PASS:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
    except: print("MQTT Error")

    print("Starting OTGW Monitor v3.8 (Stable)...")
    send_telegram("✅ Мониторинг активен (v3.8)")

    while True:
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect((OTGW_IP, OTGW_PORT))
            print("Connected to OTGW!")
            s.sendall(b"PS=1\r\n") 

            buffer = ""
            while True:
                current_time = time.time()
                
                # 1. Отчет
                if current_time - last_report_time > REPORT_INTERVAL:
                    send_status_report()
                    last_report_time = current_time

                # 2. ОПРОС (Обязателен для вашего котла!)
                if current_time - last_poll_time > POLL_INTERVAL:
                    try:
                        # Спрашиваем главные параметры
                        s.sendall(b"RR=25\r\n") # Котел
                        time.sleep(0.1)
                        s.sendall(b"RR=26\r\n") # ГВС
                        time.sleep(0.1)
                        s.sendall(b"RR=18\r\n") # Давление
                        time.sleep(0.1)
                        s.sendall(b"RR=17\r\n") # Модуляция
                    except: pass
                    last_poll_time = current_time

                # 3. Чтение
                try:
                    data = s.recv(1024)
                except socket.timeout:
                    continue

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
                        
                        if "Error" in clean_line:
                            status['errors_set'].add(clean_line)
                            if mqtt_connected: client.publish(TOPIC_ERROR, clean_line)

                except: pass

        except socket.error:
            print("Connection lost, retrying...")
            time.sleep(10)
        except Exception:
            time.sleep(10)
        finally:
            if s: s.close()

if __name__ == "__main__":
    main()