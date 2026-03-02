FROM python:3.11-slim

# Устанавливаем ffmpeg (нужен для конвертации видео)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Переменные окружения (задаются при запуске)
ENV BOT_TOKEN=""
ENV BOT_USERNAME=""

CMD ["python", "bot.py"]
