FROM python:3.11-slim

# Установка системных зависимостей (включая SQLite)
RUN apt-get update && apt-get install -y \
    gcc \
    libsqlite3-dev \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Рабочая директория
WORKDIR /app

# Копируем зависимости
COPY requirements.txt .

# Устанавливаем Python-зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY src/ .

# Создаём папку для данных (БД пользователей)
RUN mkdir -p /app/data

# Запуск бота
CMD ["python", "-u", "main.py"]

# Healthcheck (опционально)
HEALTHCHECK --interval=5m --timeout=30s \
  CMD python -c "import sqlite3; print('ok')" || exit 1