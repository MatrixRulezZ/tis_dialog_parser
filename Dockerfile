FROM python:3.11-slim

# Установка системных зависимостей
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Установка рабочей директории
WORKDIR /app

# Копируем зависимости
COPY requirements.txt .

# Устанавливаем Python зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY src/ .

# Запускаем бота
CMD ["python", "-u", "main.py"]

HEALTHCHECK --interval=5m --timeout=30s \
  CMD curl -f http://localhost:8080/health || exit 1