FROM python:3.10-slim

# Устанавливаем FFmpeg (нужен для работы с аудио в Discord)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Создаем рабочую директорию
WORKDIR /app

# Копируем зависимости и устанавливаем их
COPY requirements.txt .

# 1) Ставим voice_recv (он тянет обычный discord.py)
# 2) Затем ПЕРЕЗАПИСЫВАЕМ discord.py на discord.py-self
RUN pip install --no-cache-dir discord-ext-voice_recv==0.5.2a179 && \
    pip install --no-cache-dir --force-reinstall "discord.py-self>=2.0.0" && \
    pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY . .

# Команда для запуска бота
CMD ["python", "-u", "voice_bot.py"]
