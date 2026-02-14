#  OTGW Logger & Monitor (Ampera/Laxilef Edition)

Умный мониторинг для котлов **Baxi Ampera Pro** (и аналогов) через шлюз **OpenTherm Gateway** с прошивкой **Laxilef**.

Скрипт объединяет данные из разных источников (стандартный OpenTherm, текстовые логи датчиков Laxilef, MQTT-контекст) в единый поток данных Telegram для Home Assistant ошибка.

##  Возможности

1.  **Гибридный парсинг (v3.22):**
    * Понимает стандартные OpenTherm ID (HEX).
    * Читает именованные датчики из логов Laxilef (например, `'Heating return temp'`).
    * **Context Aware:** Распознает безымянные значения `{"value": ...}` в MQTT, сопоставляя их с предыдущей строкой топика.
2.  **Интеграция с Home Assistant:**
    * Отправляет все данные в MQTT.
    * Публикует статусы ошибок и аварий.
3.  **Telegram Бот:**
    *  Мгновенные уведомления об авариях (с расшифровкой кодов, например `17` -> `E9`).
    *  Предупреждения о падении (<0.7) или превышении (>2.8) давления.
    *  Ежечасный отчет с полным состоянием системы (Температуры, Модуляция, Ошибки).
4.  **Пассивный режим:** Не нагружает шлюз запросами, работает в режиме прослушивания (снижает нагрузку на ESP8266/WiFi).

##  Структура проекта

* `compose.yaml` — Конфигурация Docker Compose.
* `.env` — Переменные окружения (Пароли, IP, Токены).
* `Dockerfile` — Инструкция сборки образа.
* `requirements.txt` — Зависимости Python.
* `logger.py` — Основной скрипт логики.

##  Установка

1. Клонируйте репозиторий
  ```bash
git clone [https://github.com/chubaise/otgw-logger.git](https://github.com/chubaise/otgw-logger.git)
cd otgw-logger
```
2. Настройте окружение Создайте файл .env (или переименуйте env.example если есть) и укажите свои данные: 
  ```bash
  # .env файл
  OTGW_IP=192.168.1.50
  OTGW_PORT=23

  MQTT_BROKER=192.168.1.111
  MQTT_PORT=1883
  # MQTT_USER=user
  # MQTT_PASS=pass
  MQTT_TOPIC_ERROR=otgw/error

  TG_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
  TG_CHAT_ID=123456789
```
3. Запустите контейнер
  ```bash
  docker compose up -d --build
```
 Устранение неполадок
Проверьте логи контейнера:
Убедитесь, что в логах появляются зеленые строки ✅ UPDATE: ....
Если видите Connection lost, проверьте IP адрес шлюза и работу WiFi.
  ```bash
  docker compose logs -f
```






  
