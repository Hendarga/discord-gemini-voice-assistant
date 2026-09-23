FROM python:3.10-slim

# Устанавливаем FFmpeg (нужен для работы с аудио в Discord)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Создаем рабочую директорию
WORKDIR /app

# Копируем зависимости и устанавливаем их
COPY requirements.txt .

# voice_recv сначала устанавливает базовый discord.py, затем актуальный
# self-клиент заменяет его файлы в namespace discord и получает DAVE через davey.
RUN pip install --no-cache-dir "discord-ext-voice_recv @ https://github.com/imayhaveborkedit/discord-ext-voice-recv/archive/refs/heads/main.zip" && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir "davey>=0.1.0"

# Копируем исходный код
COPY . .

# Команда для запуска бота
CMD ["python", "-u", "voice_bot.py"]
