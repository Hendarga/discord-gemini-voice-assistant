"""
voice_bot.py — Голосовой + Текстовый self-bot
===============================================
Оптимизирован под Render Free (512MB RAM, 0.1 CPU)
STT: Groq Whisper API (бесплатно, 7000 req/день) или Google Speech Recognition (без ключа)
TTS: edge-tts (Microsoft, бесплатно)
AI:  Gemini API
"""

import os, asyncio, random, re, time, wave, tempfile, base64, io
from collections import defaultdict

import aiohttp
import discord
from discord.ext import voice_recv
from discord.ext.voice_recv.extras import speechrecognition as sr_ext
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────
# ПАТЧ: делаем voice_recv устойчивым к OpusError (selfbot)
# discord.py-self использует другой формат шифрования пакетов,
# из-за чего Opus декодер иногда видит "corrupted stream".
# Этот патч заменяет краш на тишину — роутер продолжает работу.
# ─────────────────────────────────────────────────────────────
def _apply_voice_recv_patch():
    try:
        from discord.ext.voice_recv import opus as vr_opus
        import discord.opus as _dopus

        _orig_decode = vr_opus.PacketDecoder._decode_packet

        def _safe_decode(self, packet):
            try:
                return _orig_decode(self, packet)
            except (_dopus.OpusError, Exception):
                # Возвращаем тишину вместо краша роутера
                return packet, bytes(7680)  # 20мс тишины 48kHz стерео 16-bit

        vr_opus.PacketDecoder._decode_packet = _safe_decode
        print("[PATCH] voice_recv Opus decoder: защита от corrupted stream активна", flush=True)
    except Exception as e:
        print(f"[PATCH] Не удалось применить патч: {e}", flush=True)

_apply_voice_recv_patch()


# ─────────────────────────────────────────────────────────────
# КОНФИГУРАЦИЯ
# ─────────────────────────────────────────────────────────────
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY",    "").strip()
GEMINI_MODEL      = os.getenv("GEMINI_MODEL",      "gemini-2.0-flash-lite").strip()
GROQ_API_KEY      = os.getenv("GROQ_API_KEY",      "").strip()   # Бесплатно на groq.com

SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT",
    "Ты — полезный ИИ-помощник. Отвечай кратко и по делу.")

OWNER_ID  = int(os.getenv("OWNER_ID",   "1121431968022798347"))
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

TRIGGER_WORDS_RAW = os.getenv("TRIGGER_WORDS", "бот,bot")
TARGET_IDS_RAW    = os.getenv("TARGET_IDS",    "")
TARGET_COOLDOWN   = int(os.getenv("TARGET_COOLDOWN", "180"))

TTS_VOICE    = os.getenv("TTS_VOICE",    "ru-RU-DariyaNeural")
TTS_RATE     = os.getenv("TTS_RATE",     "+10%")
TTS_PITCH    = os.getenv("TTS_PITCH",    "+0Hz")
TTS_ENABLED  = os.getenv("TTS_ENABLED",  "true").lower() == "true"
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "ru")   # Язык распознавания

WEB_PORT = int(os.getenv("PORT", "10000"))

# ─────────────────────────────────────────────────────────────
# СОСТОЯНИЕ
# ─────────────────────────────────────────────────────────────
bot               = discord.Client()
request_semaphore = asyncio.Semaphore(3)
processing_lock   = asyncio.Lock()

text_history: dict[int, list] = {}       # channel_id → история текста
voice_history                  = defaultdict(list)  # channel_id → история голоса

user_cooldowns:  dict[int, float] = {}
is_bot_active    = True
last_active_time = 0.0
current_status   = discord.Status.invisible
tts_voice_current = TTS_VOICE            # можно менять командой !голос

# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────
def get_trigger_words() -> list[str]:
    return [x.strip().lower() for x in TRIGGER_WORDS_RAW.split(",") if x.strip()]

def get_target_ids() -> set[int]:
    return {int(x) for x in TARGET_IDS_RAW.split(",") if x.strip().isdigit()}

def split_for_discord(text: str, limit: int = 2000) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, i = [], 0
    while i < len(text):
        cut = text.rfind("\n", i, i + limit)
        if cut <= i:
            cut = text.rfind(" ", i, i + limit)
        if cut <= i:
            cut = i + limit
        chunks.append(text[i:cut].strip())
        i = cut
    return [c for c in chunks if c]

def human_delay(_: str) -> float:
    return random.uniform(2.0, 3.0)

def clean_reply(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()
    return re.sub(r"\n{3,}", "\n\n", text)

# ─────────────────────────────────────────────────────────────
# KEEP-ALIVE (Render)
# ─────────────────────────────────────────────────────────────
async def handle_health(req):
    return web.Response(text="Voice bot is running.", status=200)

async def start_keepalive():
    app = web.Application()
    app.router.add_get("/",       handle_health)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()
    print(f"[HTTP] Keep-alive на порту {WEB_PORT}", flush=True)

async def self_ping_loop():
    url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if not url:
        return
    await asyncio.sleep(30)
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                await s.get(f"{url}/health", timeout=aiohttp.ClientTimeout(total=10))
        except Exception:
            pass
        await asyncio.sleep(300)

async def status_manager_loop():
    global current_status
    await bot.wait_until_ready()
    await bot.change_presence(status=discord.Status.invisible)
    while True:
        now = time.time()
        target = discord.Status.online if (now - last_active_time) < 300 else discord.Status.invisible
        if target != current_status:
            try:
                await bot.change_presence(status=target)
                current_status = target
            except Exception:
                pass
        await asyncio.sleep(10)

# ─────────────────────────────────────────────────────────────
# GEMINI (ТЕКСТ)
# ─────────────────────────────────────────────────────────────
async def gemini_text(history: list, override_prompt: str = None) -> str:
    if not GEMINI_API_KEY:
        return "[ОШИБКА] GEMINI_API_KEY не настроен!"

    prompt = override_prompt or SYSTEM_PROMPT
    url    = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

    payload = {
        "system_instruction": {"parts": [{"text": prompt}]},
        "contents": history,
        "generationConfig": {"maxOutputTokens": 8192},
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    try:
        async with request_semaphore:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as s:
                async with s.post(url, json=payload, headers=headers) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status != 200:
                        err = data.get("error", {}).get("message", "?")
                        print(f"[GEMINI ERROR] {resp.status}: {err}", flush=True)
                        print(f"[GEMINI ERROR] Полный ответ: {data}",  flush=True)
                        return f"[ОШИБКА API] {err}"
                    candidates = data.get("candidates", [])
                    if not candidates:
                        print(f"[GEMINI WARN] Пустые candidates: {data}", flush=True)
                        return "[ОШИБКА] Gemini вернул пустой ответ."
                    text = "".join(
                        p.get("text", "")
                        for p in candidates[0].get("content", {}).get("parts", [])
                        if "text" in p
                    ).strip()
                    return text or "..."
    except Exception as e:
        print(f"[GEMINI EXCEPTION] {e}", flush=True)
        return "[ОШИБКА] Ошибка подключения к API."

# ─────────────────────────────────────────────────────────────
# GEMINI (ГОЛОС, короткий ответ)
# ─────────────────────────────────────────────────────────────
async def gemini_voice(channel_id: int, user_text: str) -> str:
    history = voice_history[channel_id]
    history.append({"role": "user", "parts": [{"text": user_text}]})
    if len(history) > 20:
        voice_history[channel_id] = history[-20:]
        history = voice_history[channel_id]

    # Короткий промпт для голоса
    voice_prompt = (
        SYSTEM_PROMPT
        + "\n\n[ГОЛОСОВОЙ РЕЖИМ] Отвечай максимум 2-3 предложения. "
        "Не используй эмодзи, скобки и спецсимволы."
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {
        "system_instruction": {"parts": [{"text": voice_prompt}]},
        "contents": history,
        "generationConfig": {"maxOutputTokens": 300},
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.post(url, json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    err = data.get("error", {}).get("message", "?")
                    print(f"[GEMINI VOICE ERROR] {resp.status}: {err}", flush=True)
                    return "Ошибка нейросети."
                candidates = data.get("candidates", [])
                if not candidates:
                    return "Нейросеть не ответила."
                text = "".join(
                    p.get("text", "")
                    for p in candidates[0].get("content", {}).get("parts", [])
                    if "text" in p
                ).strip()
                history.append({"role": "model", "parts": [{"text": text}]})
                print(f"[VOICE AI] '{text}'", flush=True)
                return text or "..."
    except Exception as e:
        print(f"[GEMINI VOICE EXCEPTION] {e}", flush=True)
        return "Ошибка подключения."

# ─────────────────────────────────────────────────────────────
# TTS (озвучка через Google gTTS)
# ─────────────────────────────────────────────────────────────
async def synthesize(text: str) -> bytes | None:
    if not TTS_ENABLED:
        print("[TTS] TTS отключён в настройках.", flush=True)
        return None
    try:
        from gtts import gTTS
        import io
        print(f"[TTS] Генерирую аудио gTTS: '{text[:60]}...' (lang=ru)", flush=True)
        loop = asyncio.get_event_loop()
        # gTTS — синхронная библиотека, запускаем в executor чтобы не блокировать
        def _make():
            buf = io.BytesIO()
            gTTS(text=text, lang="ru", slow=False).write_to_fp(buf)
            return buf.getvalue()
        audio = await loop.run_in_executor(None, _make)
        print(f"[TTS] Готово: {len(audio)} байт", flush=True)
        return audio
    except Exception as e:
        print(f"[TTS ERROR] {type(e).__name__}: {e}", flush=True)
        return None

# ─────────────────────────────────────────────────────────────
# ВОСПРОИЗВЕДЕНИЕ В ГОЛОСОВОМ КАНАЛЕ
# ─────────────────────────────────────────────────────────────
async def play_in_vc(vc: discord.VoiceClient, audio_bytes: bytes):
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name
    done = asyncio.Event()
    loop = asyncio.get_event_loop()
    try:
        source = discord.FFmpegPCMAudio(tmp_path)

        def after(err):
            if err:
                print(f"[PLAY ERROR] {err}", flush=True)
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            # Используем loop напрямую — безопасно из другого треда
            loop.call_soon_threadsafe(done.set)

        vc.play(source, after=after)
        await asyncio.wait_for(done.wait(), timeout=120)
    except asyncio.TimeoutError:
        print("[PLAY] Таймаут воспроизведения — пропускаю", flush=True)
    except Exception as e:
        print(f"[PLAY ERROR] {e}", flush=True)
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

# ─────────────────────────────────────────────────────────────
# ГОЛОСОВЫЕ СОБЫТИЯ: следим кто зашел
# ─────────────────────────────────────────────────────────────
async def handle_recognized_speech(text_channel, user, text, vc):
    """Асинхронная обработка распознанного текста."""
    if not text or len(text.strip()) < 2:
        return

    print(f"\n[VOICE] {user.display_name} сказал: {text}", flush=True)
    try:
        await text_channel.send(f"🎤 **{user.display_name}**: {text}", delete_after=90)
    except Exception:
        pass

    reply = await gemini_voice(text_channel.id, text)
    
    if reply.startswith("[ОШИБКА") or reply.startswith("Ошибка"):
        if OWNER_ID:
            owner = bot.get_user(OWNER_ID)
            if owner:
                try:
                    await owner.send(f"⚠️ Ошибка ИИ в голосовом канале:\n```{reply}```")
                except Exception:
                    pass
        return

    try:
        await text_channel.send(f"🤖 **Бот**: {reply}", delete_after=90)
    except Exception:
        pass

    audio_bytes = await synthesize(reply)
    if audio_bytes and vc and vc.is_connected():
        await play_in_vc(vc, audio_bytes)

@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after:  discord.VoiceState
):
    """Автоматически заходим в ГС если туда зашёл target-пользователь."""
    if not is_bot_active:
        return
    # Нас не интересует выход или перемещение без захода в новый канал
    if after.channel is None:
        return
    # Сам бот — игнорируем
    if member.id == bot.user.id:
        return

    target_ids = get_target_ids()
    if member.id not in target_ids:
        return

    vc = member.guild.voice_client
    # Уже в том же канале
    if vc and vc.channel == after.channel:
        return

    print(f"[VOICE TRACK] {member.display_name} зашёл в {after.channel.name} — захожу!", flush=True)

    try:
        # Выходим из текущего канала если есть
        if vc:
            await vc.disconnect(force=True)

        new_vc = await after.channel.connect(cls=voice_recv.VoiceRecvClient)

        # Ищём текстовый канал (нужен чтобы дублировать транскрипцию)
        text_ch = member.guild.system_channel
        if not text_ch:
            text_ch = next((c for c in member.guild.text_channels if c.permissions_for(member.guild.me).send_messages), None)

        print(f"[STT] text_ch={'найден: ' + text_ch.name if text_ch else 'НЕ НАЙДЕН — слушаем молча'}", flush=True)

        # Прослушка ВСЕГДА стартует, даже если text_ch не найден
        def on_speech(user, text):
            print(f"[STT RAW] user={getattr(user,'display_name','?')} text={repr(text)}", flush=True)
            if not getattr(user, 'bot', False) and user.id != bot.user.id:
                print(f"[STT] Передаю в обработку: {user.display_name} → {text}", flush=True)
                asyncio.run_coroutine_threadsafe(
                    handle_recognized_speech(text_ch, user, text, new_vc),
                    bot.loop
                )
            else:
                print(f"[STT] Игнорирую (бот/сам): user_id={getattr(user,'id','?')}", flush=True)

        print(f"[STT] Запускаю прослушку в {after.channel.name}", flush=True)
        sink = sr_ext.SpeechRecognitionSink(
            default_recognizer='google',
            text_cb=on_speech,
            phrase_time_limit=10,
            ignore_silence_packets=True
        )
        new_vc.listen(sink)
        print(f"[STT] Прослушка запущена!", flush=True)

    except Exception as e:
        print(f"[VOICE TRACK ERROR] {e}", flush=True)

# ─────────────────────────────────────────────────────────────
# ТЕКСТОВЫЕ КОМАНДЫ
# ─────────────────────────────────────────────────────────────
async def voice_join(message: discord.Message):
    if not message.author.voice:
        await message.reply("❌ Зайди в голосовой канал!", mention_author=False)
        return
    vc = message.guild.voice_client
    if vc:
        await vc.disconnect(force=True)
    new_vc = await message.author.voice.channel.connect(cls=voice_recv.VoiceRecvClient)
    
    text_ch = message.channel
    def on_speech(user, text):
        if not getattr(user, 'bot', False) and user.id != bot.user.id:
            asyncio.run_coroutine_threadsafe(
                handle_recognized_speech(text_ch, user, text, new_vc),
                bot.loop
            )

    sink = sr_ext.SpeechRecognitionSink(default_recognizer='google', text_cb=on_speech, phrase_time_limit=10, ignore_silence_packets=True)
    new_vc.listen(sink)
    await message.reply(f"✅ Зашла в **{message.author.voice.channel.name}**! Теперь я слушаю всех.", mention_author=False)

async def voice_leave(message: discord.Message):
    vc = message.guild.voice_client
    if not vc:
        await message.reply("❌ Я не в голосовом канале.", mention_author=False)
        return
    await vc.disconnect(force=True)
    await message.reply("👋 Вышел.", mention_author=False)

async def voice_say(message: discord.Message, text: str):
    vc = message.guild.voice_client
    if not vc:
        if not message.author.voice:
            await message.reply("❌ Зайди в голосовой канал!", mention_author=False)
            return
        vc = await message.author.voice.channel.connect(cls=voice_recv.VoiceRecvClient)
        # Not starting listen here because this is just a quick say command.
        
    msg = await message.reply(f"🔊 *{text}*", mention_author=False)
    audio = await synthesize(text)
    if not audio:
        await msg.edit(content="⚠️ Ошибка TTS.")
        return
    await play_in_vc(vc, audio)

async def voice_set_voice(message: discord.Message, name: str):
    global tts_voice_current
    tts_voice_current = name.strip()
    await message.reply(f"✅ Голос: `{tts_voice_current}`", mention_author=False)

async def voice_voices(message: discord.Message):
    await message.reply("""**🎌 Голоса edge-tts:**
```
Японские (аниме):
  ja-JP-NanamiNeural  — мягкий женский
  ja-JP-AoiNeural     — молодой женский

Русские женские:
  ru-RU-SvetlanaNeural
  ru-RU-DariyaNeural

Русские мужские:
  ru-RU-DmitryNeural
```
Смена: `!голос <название>`""", mention_author=False)

async def voice_clear_history(message: discord.Message):
    voice_history.pop(message.channel.id, None)
    text_history.pop(message.channel.id,  None)
    await message.reply("🗑️ История очищена.", mention_author=False)

async def voice_help(message: discord.Message):
    await message.reply("""**🎤 Голосовой бот — команды:**
`!join` / `!войти` — зайти в голосовой канал
`!leave` / `!выйти` — выйти из голосового канала
`!say <текст>` / `!скажи <текст>` — произнести голосом
`!голос <название>` — сменить голос TTS
`!голоса` — список голосов
`!vclear` — очистить историю
`!vhelp` — эта справка

*Бот автоматически заходит в ГС к TARGET-пользователям и читает им текстовые ответы голосом.*""", mention_author=False)

# ─────────────────────────────────────────────────────────────
# ГОЛОСОВЫЕ КОМАНДЫ (роутер)
# ─────────────────────────────────────────────────────────────
VOICE_CMDS = {
    "!join":    (voice_join,         False),
    "!войти":   (voice_join,         False),
    "!leave":   (voice_leave,        False),
    "!выйти":   (voice_leave,        False),
    "!vclear":  (voice_clear_history,False),
    "!голоса":  (voice_voices,       False),
    "!vhelp":   (voice_help,         False),
    "!say":     (voice_say,          True),
    "!скажи":   (voice_say,          True),
    "!голос":   (voice_set_voice,    True),
}

# ─────────────────────────────────────────────────────────────
# ON_MESSAGE — текстовый режим + роутинг голосовых команд
# ─────────────────────────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    global is_bot_active, last_active_time

    if message.author.bot or message.author == bot.user:
        return

    content = message.content.strip()
    if not content:
        return

    # ── ЛС-команды владельца: # озвучить, % спросить ────────
    is_dm = message.guild is None
    if is_dm and message.author.id == OWNER_ID:
        if content.startswith("#"):
            # # текст — просто озвучить в ГС
            text_to_say = content[1:].strip()
            if not text_to_say:
                await message.reply("❌ Напиши текст после `#`", mention_author=False)
                return
            # Ищем активное голосовое подключение на любом сервере
            vc = None
            for guild in bot.guilds:
                if guild.voice_client and guild.voice_client.is_connected():
                    vc = guild.voice_client
                    break
            if not vc:
                await message.reply("❌ Я не в голосовом канале ни на одном сервере.", mention_author=False)
                return
            print(f"[DM #] Озвучиваю: {text_to_say}", flush=True)
            await message.reply(f"🔊 Озвучиваю в **{vc.channel.name}**", mention_author=False)
            audio = await synthesize(text_to_say)
            if audio:
                await play_in_vc(vc, audio)
            else:
                await message.reply("⚠️ Ошибка TTS.", mention_author=False)
            return

        elif content.startswith("%"):
            # % вопрос — спросить ИИ и озвучить ответ в ГС
            question = content[1:].strip()
            if not question:
                await message.reply("❌ Напиши вопрос после `%`", mention_author=False)
                return
            vc = None
            for guild in bot.guilds:
                if guild.voice_client and guild.voice_client.is_connected():
                    vc = guild.voice_client
                    break
            if not vc:
                await message.reply("❌ Я не в голосовом канале ни на одном сервере.", mention_author=False)
                return
            print(f"[DM %] Вопрос: {question}", flush=True)
            await message.reply(f"🤔 Думаю...", mention_author=False)
            reply = await gemini_voice(vc.channel.id, question)
            if reply.startswith("[ОШИБКА") or reply.startswith("Ошибка"):
                await message.reply(f"⚠️ {reply}", mention_author=False)
                return
            await message.reply(f"🔊 Отвечаю в **{vc.channel.name}**: {reply}", mention_author=False)
            audio = await synthesize(reply)
            if audio:
                await play_in_vc(vc, audio)
            return

    # ── Голосовые команды (приоритет) ──────────────────────
    parts   = content.split(maxsplit=1)
    cmd_key = parts[0].lower()
    arg     = parts[1] if len(parts) > 1 else ""

    if cmd_key in VOICE_CMDS:
        handler, needs_arg = VOICE_CMDS[cmd_key]
        if needs_arg:
            if not arg:
                await message.reply(f"❌ Укажи аргумент: `{cmd_key} <текст>`", mention_author=False)
                return
            await handler(message, arg)
        else:
            await handler(message)
        return

    # ── Базовые управляющие команды ──────────────────────────
    cmd_lower = content.lower()
    if cmd_lower in ("!stop", "!start", "!clear"):
        if message.author.id not in ADMIN_IDS:
            return
        if cmd_lower == "!stop":
            is_bot_active = False
            await message.reply("Бот в спящем режиме.", mention_author=False)
        elif cmd_lower == "!start":
            is_bot_active = True
            await message.reply("Бот активен.", mention_author=False)
        elif cmd_lower == "!clear":
            text_history.pop(message.channel.id, None)
            await message.reply("История чата стерта.", mention_author=False)
        return

    if not is_bot_active:
        return

    # ── Текстовый ИИ-режим ──────────────────────────────────
    is_dm        = message.guild is None
    content_low  = content.lower()
    target_ids   = get_target_ids()
    trigger_words = get_trigger_words()

    has_trigger    = any(w in content_low for w in trigger_words)
    is_target_user = message.author.id in target_ids

    # Кулдаун для целей
    if is_target_user:
        now  = time.time()
        last = user_cooldowns.get(message.author.id, 0)
        if now - last < TARGET_COOLDOWN:
            return
        user_cooldowns[message.author.id] = now

    # Режим владельца
    is_owner_cmd   = False
    custom_prompt  = None

    if message.author.id == OWNER_ID and OWNER_ID != 0:
        if content.startswith("!"):
            is_owner_cmd = True
            content      = content[1:].strip()
        elif content.startswith("~"):
            is_owner_cmd = True
            content      = content[1:].strip()
            try:
                with open(__file__, "r", encoding="utf-8") as f:
                    current_code = f.read()
            except Exception:
                current_code = "# не удалось прочитать"
            custom_prompt = (
                "Ты — системный ИИ-ассистент. Выйди из роли полностью.\n"
                "Ты можешь изменять свой код через update_bot_code.\n\n"
                f"ТЕКУЩИЙ КОД:\n```python\n{current_code}\n```\n\n"
                "ПРАВИЛА:\n"
                "1. НЕ вызывай update_bot_code сразу — сначала опиши изменения и спроси «Подтверждаешь?».\n"
                "2. Вызывай только после подтверждения.\n"
                "3. Передавай ПОЛНЫЙ код, не огрызок."
            )

    if not (is_dm or has_trigger or is_target_user or is_owner_cmd):
        return

    last_active_time = time.time()

    ch_id   = message.channel.id
    history = text_history.setdefault(ch_id, [])

    user_name = message.author.display_name
    ai_input  = f"[{user_name} | {message.author.id}]: {content}"
    if is_target_user:
        ai_input += "\n[СИСТЕМНОЕ ПРАВИЛО: ОТВЕТЬ В 3-4 ПРЕДЛОЖЕНИЯ]"

    history.append({"role": "user", "parts": [{"text": ai_input}]})
    if len(history) > 20:
        text_history[ch_id] = history[-20:]
        history = text_history[ch_id]

    async with message.channel.typing():
        await asyncio.sleep(human_delay(content))
        answer = await gemini_text(history, override_prompt=custom_prompt)

    # Уведомление владельца об ошибке
    if answer.startswith("[ОШИБКА") or answer.startswith("Ошибка"):
        if OWNER_ID:
            owner = bot.get_user(OWNER_ID)
            if owner:
                try:
                    await owner.send(f"⚠️ Ошибка ИИ в текстовом канале:\n```{answer}```")
                except Exception:
                    pass
        return  # Прерываем выполнение, НЕ ОТПРАВЛЯЕМ ошибку в публичный чат!

    answer = clean_reply(answer)
    history.append({"role": "model", "parts": [{"text": answer}]})

    print(f"\n[TEXT] Канал:{ch_id} → {message.author.display_name}", flush=True)
    print(f"Ответ: {answer}\n{'─'*40}", flush=True)

    # ДЕБАГ ФИЧА / ГОЛОСОВОЙ ОТВЕТ НА ТЕКСТ ДЛЯ ЦЕЛЕЙ:
    # Если пишет target_user, и бот находится с ним в голосовом канале — озвучиваем ответ голосом!
    if is_target_user and message.author.voice:
        vc = message.guild.voice_client
        if vc and vc.channel == message.author.voice.channel:
            print(f"[DEBUG MODE] Цель написала текст, бот в том же ГС. Озвучиваю ответ голосом!", flush=True)
            audio = await synthesize(answer)
            if audio:
                await play_in_vc(vc, audio)
                # Отправляем в чат с пометкой, что ответили голосом
                await message.reply(f"🔊 *(Ответила голосом)* {answer}", mention_author=False)
                return

    # Обычный текстовый ответ
    chunks = split_for_discord(answer)
    for i, chunk in enumerate(chunks):
        if i == 0:
            await (message.channel.send(chunk) if is_dm else message.reply(chunk, mention_author=False))
        else:
            await asyncio.sleep(random.uniform(0.3, 0.8))
            await message.channel.send(chunk)

# ─────────────────────────────────────────────────────────────
# ON_READY
# ─────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    global tts_voice_current
    print(f"[BOT] Запущен: {bot.user}", flush=True)
    print(f"[BOT] TTS: {tts_voice_current} | STT: Groq/Google", flush=True)
    asyncio.create_task(start_keepalive())
    asyncio.create_task(self_ping_loop())
    asyncio.create_task(status_manager_loop())

# ─────────────────────────────────────────────────────────────
# ЗАПУСК
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("[ОШИБКА] DISCORD_BOT_TOKEN не указан!")
    bot.run(DISCORD_BOT_TOKEN)
