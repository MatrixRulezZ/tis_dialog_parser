# Используем официальный образ Python
FROM python:3.11-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем зависимости
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY src/ .

# Устанавливаем переменные окружения по умолчанию
ENV BOT_TOKEN="5660544168:AAEm0W-cbpR3L_8MKUINp7mzgG1d2mb7pT8"
ENV CHAT_ID="425457895"

# Запускаем бота
CMD ["python", "-u", "main.py"]