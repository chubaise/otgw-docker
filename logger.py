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

LOG_DIR = "/logs"
ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

logger = logging.getLogger("OTGW")
logger.setLevel(logging.INFO)
hourly_handler = TimedRotatingFileHandler(f"{LOG_DIR}/otgw_hourly.log", when="h", interval=1, backupCount=168)
hourly_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logger.addHandler(hourly_handler)
daily_handler = TimedRotatingFileHandler(f"{LOG_DIR}/otgw_daily.log", when="midnight", interval=1, backupCount=30)
daily_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logger.addHandler(daily_handler)

mqtt_connected = False

def send_telegram(message):
    if TG_TOKEN and TG_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
            prefix = "⚠️ Авария котла:\n" if "Error" in message else "ℹ️ OTGW: "
            requests.post(url, json={"chat_id": TG_CHAT_ID, "text": f"{prefix}{message}"}, timeout=5)
        except Exception:
            pass

def on_connect(client, userdata, flags, rc):
    global mqtt_connected
    if rc == 0:
        print("Connected to MQTT!")
        mqtt_connected = True

def main():
    client = mqtt.Client()
    if MQTT_USER and MQTT_PASS:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
    except Exception:
        print("MQTT Error")

    print("Starting...")
    send_telegram("Сервис мониторинга перезапущен. Подключение...")

    while True:
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10)
            s.connect((OTGW_IP, OTGW_PORT))
            print("Connected to OTGW!")
            send_telegram("✅ Связь с котлом установлена!")

            buffer = ""
            while True:
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
                        if "Error" in clean_line:
                            print(f"ERROR: {clean_line}")
                            if mqtt_connected: client.publish(TOPIC_ERROR, clean_line)
                            send_telegram(clean_line)
                except Exception: pass
        except socket.error:
            send_telegram("❌ Потеряна связь с шлюзом")
        except Exception: pass
        finally:
            if s: s.close()
            time.sleep(5)

if __name__ == "__main__":
    main()