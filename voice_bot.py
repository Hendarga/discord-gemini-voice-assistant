import os
import asyncio
import io
import wave
import json
import numpy as np
import discord
from discord.ext import commands
import google.generativeai as genai
from google.genai import types
import edge_tts
import whisper

# --- КОНФИГУРАЦИЯ ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")

OWNER_ID = 1121431968022798347           # Твой ID для получения логов в ЛС
TARGET_GUILD_ID = 1527454812260532306    # ID целевого сервера

TTS_VOICE = "ru-RU-DmitryNeural"
WHISPER_MODEL_NAME = "base"

SYSTEM_PROMPT = """Ты — Губка Боб Квадратные Штаны.
Ты находишься в голосовом канале Discord. Отвечай коротко, задорно, используй фирменный юмор и эмоции.
Не пиши длинные тексты — твой ответ сразу озвучивается в голосовой канал."""

# Инициализация Gemini
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel(
    model_name="gemini-2.5-flash",
    system_instruction=SYSTEM_PROMPT
)

# Загрузка Whisper
print("[INIT] Загрузка модели Whisper...")
whisper_model = whisper.load_model(WHISPER_MODEL_NAME)
print("[INIT] Whisper готов к работе.")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

chat_histories = {}
voice_lock = asyncio.Lock()


async def get_owner_dm():
    """Получает объект пользователя для отправки сообщений в ЛС."""
    owner = bot.get_user(OWNER_ID)
    if not owner:
        try:
            owner = await bot.fetch_user(OWNER_ID)
        except Exception as e:
            print(f"[ERROR] Не удалось найти владельца {OWNER_ID}: {e}", flush=True)
            return None
    return owner


async def gemini_voice(user_id: int, text: str, user_name: str) -> str:
    if user_id not in chat_histories:
        chat_histories[user_id] = gemini_model.start_chat(history=[])
    chat = chat_histories[user_id]
    
    prompt = f"Пользователь {user_name} сказал в ГС: {text}"
    try:
        response = await asyncio.to_thread(chat.send_message, prompt)
        return response.text.strip()
    except Exception as e:
        print(f"[GEMINI ERROR] {e}", flush=True)
        return f"[ОШИБКА ИИ]: {e}"


async def synthesize(text: str) -> bytes:
    try:
        communicate = edge_tts.Communicate(text, TTS_VOICE)
        audio_data = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_data.extend(chunk["data"])
        return bytes(audio_data)
    except Exception as e:
        print(f"[TTS ERROR] {e}", flush=True)
        return b""


async def play_in_vc(vc: discord.VoiceClient, audio_bytes: bytes):
    if not audio_bytes:
        return
    
    input_stream = io.BytesIO(audio_bytes)
    source = discord.FFmpegPCMAudio(input_stream, pipe=True)
    
    if vc.is_playing():
        vc.stop()
        
    vc.play(source)
    while vc.is_playing():
        await asyncio.sleep(0.1)


async def handle_recognized_speech(user: discord.User, text: str, vc: discord.VoiceClient):
    if not text or len(text.strip()) < 2:
        return

    async with voice_lock:
        owner = await get_owner_dm()

        # 1. Отправка распознанной речи в ЛС
        if owner:
            try:
                await owner.send(f"🎤 **[ГС] {user.display_name}**: {text}")
            except Exception as e:
                print(f"[DM ERROR] Не удалось отправить в ЛС: {e}", flush=True)

        # 2. Запрос к Gemini
        reply = await gemini_voice(OWNER_ID, text, user.display_name)

        if reply.startswith("[ОШИБКА"):
            if owner:
                try:
                    await owner.send(f"⚠️ Ошибка ИИ:\n```{reply}```")
                except Exception:
                    pass
            return

        # 3. Отправка ответа бота в ЛС
        if owner:
            try:
                await owner.send(f"🧽 **Губка Боб**: {reply}")
            except Exception:
                pass

        # 4. Озвучка в голосовой канал
        audio_bytes = await synthesize(reply)
        if audio_bytes and vc and vc.is_connected():
            await play_in_vc(vc, audio_bytes)


class WhisperAudioSink(discord.AudioSink):
    """Сборщик аудиопотока из голосового канала."""
    def __init__(self, vc: discord.VoiceClient):
        super().__init__()
        self.vc = vc
        self.user_buffers = {}
        self.loop = asyncio.get_event_loop()

    def write(self, user, data):
        if user is None:
            return
        
        pcm_data = data.pcm
        if user.id not in self.user_buffers:
            self.user_buffers[user.id] = bytearray()
            
        self.user_buffers[user.id].extend(pcm_data)

        # Если накоплено ~3 секунды аудио (48000 Hz * 2 ch * 2 bytes * 3 sec = 576000 bytes)
        if len(self.user_buffers[user.id]) >= 576000:
            raw_pcm = bytes(self.user_buffers[user.id])
            self.user_buffers[user.id].clear()
            
            self.loop.create_task(self._process_user_audio(user, raw_pcm))

    async def _process_user_audio(self, user, pcm_bytes):
        # Преобразование PCM в WAV для Whisper
        wav_io = io.BytesIO()
        with wave.open(wav_io, 'wb') as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(48000)
            wf.writeframes(pcm_bytes)
        
        wav_io.seek(0)
        
        # Запуск Whisper в отдельном потоке
        def _transcribe():
            audio_np = np.frombuffer(wav_io.read(), dtype=np.int16).astype(np.float32) / 32768.0
            result = whisper_model.transcribe(audio_np, language="ru", fp16=False)
            return result.get("text", "")

        recognized_text = await asyncio.to_thread(_transcribe)
        
        if recognized_text.strip():
            await handle_recognized_speech(user, recognized_text.strip(), self.vc)


@bot.event
async def on_ready():
    print(f"✅ Бот запущен как {bot.user} (ID: {bot.user.id})", flush=True)
    guild = bot.get_guild(TARGET_GUILD_ID)
    if guild:
        print(f"📌 Подключено к целевому серверу: {guild.name} ({guild.id})", flush=True)
    else:
        print(f"⚠️ Сервер {TARGET_GUILD_ID} не найден. Проверь наличие бота на сервере.", flush=True)


@bot.command(name="join")
async def join_vc(ctx):
    """Подключение к голосовому каналу команды."""
    if not ctx.author.voice:
        await ctx.send("Зайди в голосовой канал!")
        return

    channel = ctx.author.voice.channel
    vc = await channel.connect(cls=discord.VoiceClient)
    
    # Запуск прослушивания через WhisperAudioSink
    vc.start_recording(
        WhisperAudioSink(vc),
        lambda e: print(f"Recording stopped: {e}"),
        ctx.channel
    )
    
    owner = await get_owner_dm()
    if owner:
        await owner.send(f"🟢 Подключился к ГС **{channel.name}** на сервере `{ctx.guild.name}`. Вся переписка будет здесь.")


@bot.command(name="leave")
async def leave_vc(ctx):
    """Отключение от голосового канала."""
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        owner = await get_owner_dm()
        if owner:
            await owner.send("🔴 Отключился от голосового канала.")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)