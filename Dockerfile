FROM python:3.11-alpine

WORKDIR /app

# 1. Копируем список библиотек
COPY requirements.txt .

# 2. Устанавливаем их (замена ручному запуску pip)
RUN pip install --no-cache-dir -r requirements.txt

# 3. Копируем скрипт
COPY logger.py .

# 4. Создаем папку логов
RUN mkdir /logs

# 5. Команда запуска
CMD ["python", "-u", "logger.py"]