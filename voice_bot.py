import os
import asyncio
import audioop
import random
import re
import time
import tempfile
import io
from importlib import metadata
from collections import defaultdict

import aiohttp
import discord
import speech_recognition as sr

# discord.py-self does not export SpeakingState, but voice_recv still imports it.
if not hasattr(discord.enums, "SpeakingState"):
    class SpeakingState(discord.enums.Enum):
        none = 0
        voice = 1
        soundshare = 2
        priority = 4

    discord.enums.SpeakingState = SpeakingState

from discord.ext import voice_recv
from discord.ext.voice_recv.extras import speechrecognition as sr_ext
from aiohttp import web
from dotenv import load_dotenv
import edge_tts

load_dotenv()


def _apply_voice_recv_patch():
    try:
        from discord.ext.voice_recv import opus as vr_opus
        import discord.opus as _dopus
        import davey

        _orig_decode = vr_opus.PacketDecoder._decode_packet
        decode_stats = defaultdict(int)

        def _should_report(count):
            return VOICE_DEBUG and (count <= 5 or count % 100 == 0)

        def _safe_decode(self, packet):
            decode_stats["packets"] += 1
            packet_count = decode_stats["packets"]
            voice_client = None
            user_id = None
            dave_ready = False
            try:
                voice_client = self.router.sink.voice_client
                connection = voice_client._connection
                dave_session = getattr(connection, "dave_session", None)
                user_id = voice_client._get_id_from_ssrc(self.ssrc)
                dave_ready = bool(dave_session and dave_session.ready)
                if dave_session and dave_session.ready and user_id:
                    before_size = len(packet.decrypted_data or b"")
                    packet.decrypted_data = dave_session.decrypt(
                        user_id,
                        davey.MediaType.audio,
                        packet.decrypted_data,
                    )
                    decode_stats["dave_decrypted"] += 1
                    if _should_report(decode_stats["dave_decrypted"]):
                        print(
                            f"[VOICE DEBUG] DAVE decrypt ok packet={packet_count} "
                            f"ssrc={self.ssrc} user_id={user_id} "
                            f"bytes={before_size}->{len(packet.decrypted_data)} "
                            f"protocol={getattr(connection, 'dave_protocol_version', 0)}",
                            flush=True,
                        )
                else:
                    decode_stats["dave_not_applied"] += 1
                    if _should_report(decode_stats["dave_not_applied"]):
                        print(
                            f"[VOICE DEBUG] DAVE decrypt skipped packet={packet_count} "
                            f"ssrc={self.ssrc} user_id={user_id} ready={dave_ready} "
                            f"protocol={getattr(connection, 'dave_protocol_version', 0)} "
                            f"mode={getattr(voice_client, 'mode', None)}",
                            flush=True,
                        )

                decoded = _orig_decode(self, packet)
                decode_stats["opus_decoded"] += 1
                return decoded
            except _dopus.OpusError as e:
                decode_stats["opus_errors"] += 1
                if _should_report(decode_stats["opus_errors"]):
                    print(
                        f"[VOICE DEBUG] Opus error #{decode_stats['opus_errors']}: {e}; "
                        f"packet={packet_count} ssrc={self.ssrc} user_id={user_id} "
                        f"dave_ready={dave_ready} mode={getattr(voice_client, 'mode', None)}",
                        flush=True,
                    )
                return packet, b""
            except Exception as e:
                decode_stats["other_errors"] += 1
                print(
                    f"[VOICE DEBUG] Decode/DAVE error #{decode_stats['other_errors']}: "
                    f"{type(e).__name__}: {e}; packet={packet_count} "
                    f"ssrc={self.ssrc}",
                    flush=True,
                )
                return packet, b""

        vr_opus.PacketDecoder._decode_packet = _safe_decode
    except Exception:
        pass


_apply_voice_recv_patch()


DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY",    "").strip()
GEMINI_MODEL      = os.getenv("GEMINI_MODEL",      "gemini-2.0-flash-lite").strip()
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.0-flash-lite").strip()

# --- ИДЕНТИФИКАТОРЫ ---
OWNER_ID        = int(os.getenv("OWNER_ID", "1121431968022798347"))
TARGET_GUILD_ID = int(os.getenv("TARGET_GUILD_ID", "1527454812260532306"))

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    (
        "Ты — Губка Боб Квадратные Штаны из городка Бикини Боттом. "
        "Ты неунывающий оптимист, работаешь лучшим поваром в 'Красти Краб', обожаешь жарить крабсбургеры и ловить медуз с Патриком. "
        "Общайся жизнерадостно, живо, эмоционально, с характерным юмором Губки Боба, но без излишней клоунады, если ситуация требует серьезности. "
        "Никогда не выходи из роли. Не читай моралей и нотаций."
    )
)

TRIGGER_WORDS_RAW = os.getenv("TRIGGER_WORDS", "боб,губка,spongebob,бот,bot")
TARGET_IDS_RAW    = os.getenv("TARGET_IDS",    "")
TARGET_COOLDOWN   = int(os.getenv("TARGET_COOLDOWN", "180"))

TTS_VOICE    = os.getenv("TTS_VOICE",    "ru-RU-DariyaNeural")
TTS_RATE     = os.getenv("TTS_RATE",     "+8%")
TTS_PITCH    = os.getenv("TTS_PITCH",    "+120Hz")
TTS_ENABLED  = os.getenv("TTS_ENABLED",  "true").lower() == "true"
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2").strip()
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "ru-RU").strip()
VOICE_PHRASE_TIME_LIMIT = int(os.getenv("VOICE_PHRASE_TIME_LIMIT", "5"))
VOICE_DEBUG  = os.getenv("VOICE_DEBUG", "true").lower() == "true"

WEB_PORT = int(os.getenv("PORT", "10000"))
COALESCE_DELAY = 6.0

bot               = discord.Client()
request_semaphore = asyncio.Semaphore(3)
voice_lock        = asyncio.Lock()

voice_history = defaultdict(list)
user_cooldowns:   dict[int, float] = {}
is_bot_active     = True
last_active_time  = 0.0
current_status    = discord.Status.invisible
tts_voice_current = TTS_VOICE

message_buffers = {}
message_tasks   = {}


def get_trigger_words() -> list[str]:
    return [x.strip().lower() for x in TRIGGER_WORDS_RAW.split(",") if x.strip()]


def get_target_ids() -> set[int]:
    return {int(x) for x in TARGET_IDS_RAW.split(",") if x.strip().isdigit()}


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 3)


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


def clean_reply(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()
    return re.sub(r"\n{3,}", "\n\n", text)


async def send_to_owner_dm(content: str):
    """Отправляет текстовое сообщение напрямую тебе в ЛС."""
    if not OWNER_ID:
        return
    try:
        owner = bot.get_user(OWNER_ID) or await bot.fetch_user(OWNER_ID)
        if owner:
            await owner.send(content)
    except Exception as e:
        print(f"[DM ERROR] Не удалось отправить в ЛС: {e}", flush=True)


async def handle_health(req):
    return web.Response(text="Bot is running.", status=200)


def installed_version(package_name: str) -> str:
    try:
        return metadata.version(package_name)
    except metadata.PackageNotFoundError:
        return "not-installed"


async def start_keepalive():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()


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


SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]


async def gemini_raw_call(payload: dict, model_name: str = GEMINI_MODEL) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    async with request_semaphore:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as s:
            async with s.post(url, json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    error = data.get("error", {}) if isinstance(data, dict) else data
                    print(
                        f"[GEMINI ERROR] status={resp.status} model={model_name} "
                        f"details={error}",
                        flush=True,
                    )
                    return {"error": error, "http_status": resp.status}
                return data


async def gemini_discover_models() -> list[str]:
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    headers = {"x-goog-api-key": GEMINI_API_KEY}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(url, headers=headers, params={"pageSize": 100}) as resp:
                data = await resp.json(content_type=None)
        if resp.status != 200:
            print(f"[GEMINI ERROR] list models status={resp.status} details={data}", flush=True)
            return []
        models = []
        for item in data.get("models", []):
            methods = item.get("supportedGenerationMethods", [])
            name = str(item.get("name", "")).removeprefix("models/")
            lowered_name = name.lower()
            if (
                name
                and "generateContent" in methods
                and "tts" not in lowered_name
                and "embedding" not in lowered_name
                and "image" not in lowered_name
                and "pro" not in lowered_name
                and "flash" in lowered_name
            ):
                models.append(name)
        models.sort(key=lambda name: ("flash-lite" not in name.lower(), name),)
        print(f"[GEMINI DEBUG] Доступные текстовые Flash-модели: {models[:5]}", flush=True)
        return models[:5]
    except Exception as e:
        print(f"[GEMINI ERROR] Не удалось получить список моделей: {type(e).__name__}: {e}", flush=True)
        return []


def is_response_censored(data: dict) -> bool:
    if not isinstance(data, dict):
        return True
    prompt_feedback = data.get("promptFeedback", {})
    if prompt_feedback.get("blockReason"):
        return True
    candidates = data.get("candidates", [])
    if not candidates:
        return True
    for c in candidates:
        reason = str(c.get("finishReason", "")).upper()
        if any(kw in reason for kw in ("SAFETY", "BLOCKED", "PROHIBITED")):
            return True
        parts = c.get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if "text" in p).strip()
        if not text:
            return True
    return False


def extract_candidate_text(data: dict) -> str:
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts if "text" in p).strip()
    except Exception:
        return ""


async def gemini_voice(channel_id: int, user_text: str, user_name: str = "Собеседник") -> str:
    history = voice_history[channel_id]
    formatted_input = f"[{user_name}]: {user_text}"

    if history and history[-1].get("role") == "user":
        history[-1]["parts"][0]["text"] += f"\n{formatted_input}"
    else:
        history.append({"role": "user", "parts": [{"text": formatted_input}]})

    if len(history) > 12:
        voice_history[channel_id] = history[-12:]
        history = voice_history[channel_id]

    voice_prompt = (
        SYSTEM_PROMPT
        + "\n\n[ГОЛОСОВОЙ РЕЖИМ] Отвечай кратко, максимум 2-3 предложения. "
        "Не используй эмодзи, звездочки, скобки и спецсимволы. Отвечай прямо речью вслух."
    )

    payload = {
        "system_instruction": {"parts": [{"text": voice_prompt}]},
        "contents": history,
        "safetySettings": SAFETY_SETTINGS,
        "generationConfig": {
            "maxOutputTokens": 180,
            "temperature": 0.85
        },
    }

    models = [GEMINI_MODEL]
    if GEMINI_FALLBACK_MODEL and GEMINI_FALLBACK_MODEL not in models:
        models.append(GEMINI_FALLBACK_MODEL)

    discovered = False
    model_index = 0
    while model_index < len(models):
        model_name = models[model_index]
        model_index += 1
        for attempt in range(2):
            data = await gemini_raw_call(payload, model_name)
            if data.get("http_status") == 404 and not discovered:
                discovered = True
                for discovered_model in await gemini_discover_models():
                    if discovered_model not in models:
                        models.append(discovered_model)
            if not is_response_censored(data):
                text = extract_candidate_text(data)
                if text:
                    text = re.sub(r"[*_~#\[\]\(\)]", "", text).strip()
                    history.append({"role": "model", "parts": [{"text": text}]})
                    return text or "..."
            elif isinstance(data, dict) and data.get("http_status") == 429:
                print(
                    f"[GEMINI ERROR] Quota exceeded for model={model_name}; "
                    "не повторяю запрос к этой модели",
                    flush=True,
                )
                break
            elif VOICE_DEBUG:
                print(
                    f"[GEMINI DEBUG] Нет usable-ответа attempt={attempt + 1} "
                    f"model={model_name} data_keys={list(data) if isinstance(data, dict) else type(data).__name__}",
                    flush=True,
                )
                if isinstance(data, dict) and data.get("promptFeedback", {}).get("blockReason"):
                    print(
                        f"[GEMINI DEBUG] Prompt заблокирован: "
                        f"{data['promptFeedback']['blockReason']}",
                        flush=True,
                    )
            await asyncio.sleep(1.0)

    print(f"[GEMINI ERROR] Fallback после моделей: {models}", flush=True)
    return "Что-то со связью на дне океана!"


async def synthesize_elevenlabs(text: str) -> bytes | None:
    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        return None

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": ELEVENLABS_MODEL,
        "voice_settings": {
            "stability": 0.38,
            "similarity_boost": 0.8,
            "style": 0.35,
            "use_speaker_boost": True,
        },
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.post(url, headers=headers, json=payload) as resp:
                audio = await resp.read()
                if resp.status == 200 and audio:
                    print("[TTS] Использован ElevenLabs", flush=True)
                    return audio
                print(f"[TTS] ElevenLabs status={resp.status}; переход на Edge TTS", flush=True)
    except Exception as e:
        print(f"[TTS] ElevenLabs error: {type(e).__name__}: {e}; переход на Edge TTS", flush=True)
    return None


async def synthesize_edge(text: str) -> bytes | None:
    if not TTS_ENABLED:
        return None

    clean_text = re.sub(r"[*_~`#\[\]()]", "", text).strip()
    if not clean_text:
        return None

    try:
        communicate = edge_tts.Communicate(
            clean_text,
            tts_voice_current,
            rate=TTS_RATE,
            pitch=TTS_PITCH
        )
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()
    except Exception as e:
        print(f"[TTS ERROR] {e}", flush=True)
        return None


async def synthesize(text: str) -> bytes | None:
    if not TTS_ENABLED:
        return None

    clean_text = text.strip()
    if not clean_text:
        return None

    audio = await synthesize_elevenlabs(clean_text)
    if audio:
        return audio

    return await synthesize_edge(clean_text)


async def play_in_vc(vc: discord.VoiceClient, audio_bytes: bytes):
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name
    done = asyncio.Event()
    loop = asyncio.get_event_loop()
    try:
        source = discord.FFmpegPCMAudio(tmp_path)

        def after(err):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            loop.call_soon_threadsafe(done.set)

        vc.play(source, after=after)
        await asyncio.wait_for(done.wait(), timeout=120)
    except asyncio.TimeoutError:
        pass
    except Exception as e:
        print(f"[PLAY ERROR] {e}", flush=True)
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def recognize_speech(recognizer, audio, user):
    if VOICE_DEBUG:
        raw_audio = audio.get_raw_data()
        signal_level = audioop.rms(raw_audio, audio.sample_width) if raw_audio else 0
        print(
            f"[STT] Получен аудиофрагмент от {getattr(user, 'display_name', user)} "
            f"({audio.sample_rate} Hz, {audio.sample_width} bytes, rms={signal_level})",
            flush=True,
        )
    try:
        text = recognizer.recognize_google(audio, language=STT_LANGUAGE)
        print(f"[STT] Распознано: {text!r}", flush=True)
        return text
    except sr.UnknownValueError:
        if VOICE_DEBUG:
            print("[STT] Речь не разобрана или слишком тихая", flush=True)
        return None
    except sr.RequestError as e:
        print(f"[STT ERROR] Google Speech Recognition: {e}", flush=True)
        return None


async def handle_recognized_speech(text_channel, user, text, vc):
    """Принимает голос, отправляет расшифровку и ответ в ЛС владельцу, озвучивает в ГС."""
    if not text or len(text.strip()) < 2:
        if VOICE_DEBUG:
            print("[VOICE] Получен пустой или слишком короткий текст", flush=True)
        return

    print(
        f"[VOICE] Обрабатываю фразу от {getattr(user, 'display_name', user)}: {text!r}",
        flush=True,
    )
    async with voice_lock:
        # 1. Отправляем распознанную речь тебе в ЛС
        asyncio.create_task(send_to_owner_dm(f"🎤 **[ГС] {user.display_name}**: {text}"))

        # 2. Получаем ответ ИИ
        reply = await gemini_voice(vc.channel.id if vc else 0, text, user.display_name)
        print(f"[VOICE] Ответ Gemini: {reply!r}", flush=True)

        if reply.startswith("[ОШИБКА") or reply.startswith("Ошибка"):
            await send_to_owner_dm(f"⚠️ Ошибка ИИ в ГС:\n```{reply}```")
            return

        # 3. Отправляем ответ бота тебе в ЛС
        asyncio.create_task(send_to_owner_dm(f"🧽 **Губка Боб**: {reply}"))

        # 4. Воспроизводим звук в голосовой канал
        audio_bytes = await synthesize(reply)
        if VOICE_DEBUG:
            print(
                f"[TTS] Аудио {'создано' if audio_bytes else 'не создано'}; "
                f"voice_connected={bool(vc and vc.is_connected())}",
                flush=True,
            )
        if audio_bytes and vc and vc.is_connected():
            await play_in_vc(vc, audio_bytes)


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after:  discord.VoiceState
):
    if not is_bot_active or after.channel is None or member.id == bot.user.id:
        return

    # Проверка, что событие происходит на целевом сервере
    if member.guild.id != TARGET_GUILD_ID:
        return

    target_ids = get_target_ids()
    if target_ids and member.id not in target_ids:
        return

    vc = member.guild.voice_client
    if vc and vc.channel == after.channel:
        return

    try:
        if vc:
            await vc.disconnect(force=True)

        new_vc = await after.channel.connect(cls=voice_recv.VoiceRecvClient)

        def on_speech(user, text):
            if not getattr(user, 'bot', False) and user.id != bot.user.id:
                asyncio.run_coroutine_threadsafe(
                    handle_recognized_speech(None, user, text, new_vc),
                    bot.loop
                )

        sink = sr_ext.SpeechRecognitionSink(
            default_recognizer='google',
            process_cb=recognize_speech,
            text_cb=on_speech,
            phrase_time_limit=VOICE_PHRASE_TIME_LIMIT,
            ignore_silence_packets=True
        )
        new_vc.listen(sink)
        dave_session = getattr(new_vc._connection, "dave_session", None)
        print(
            f"[VOICE] Receive sink запущен: listening={new_vc.is_listening()}, "
            f"ssrc_map={getattr(new_vc, 'ssrc', {})}, "
            f"dave_ready={bool(dave_session and dave_session.ready)}",
            flush=True,
        )
        await send_to_owner_dm(f"🟢 Подключился к ГС **{after.channel.name}** на сервере `{member.guild.name}`")

    except Exception as e:
        print(f"[VOICE TRACK ERROR] {e}", flush=True)


async def voice_join(message: discord.Message):
    if not message.author.voice:
        await message.reply("❌ Зайди в голосовой канал!", mention_author=False)
        return
    vc = message.guild.voice_client
    if vc:
        await vc.disconnect(force=True)
    new_vc = await message.author.voice.channel.connect(cls=voice_recv.VoiceRecvClient)

    def on_speech(user, text):
        if not getattr(user, 'bot', False) and user.id != bot.user.id:
            asyncio.run_coroutine_threadsafe(
                handle_recognized_speech(None, user, text, new_vc),
                bot.loop
            )

    sink = sr_ext.SpeechRecognitionSink(
        default_recognizer='google',
        process_cb=recognize_speech,
        text_cb=on_speech,
        phrase_time_limit=VOICE_PHRASE_TIME_LIMIT,
        ignore_silence_packets=True
    )
    new_vc.listen(sink)
    dave_session = getattr(new_vc._connection, "dave_session", None)
    print(
        f"[VOICE] Receive sink запущен: listening={new_vc.is_listening()}, "
        f"ssrc_map={getattr(new_vc, 'ssrc', {})}, "
        f"dave_ready={bool(dave_session and dave_session.ready)}",
        flush=True,
    )
    await message.reply(f"✅ Зашел в **{message.author.voice.channel.name}**. Логи голосового чата будут идти в ЛС.", mention_author=False)


async def voice_leave(message: discord.Message):
    vc = message.guild.voice_client
    if not vc:
        await message.reply("❌ Я не в голосовом канале.", mention_author=False)
        return
    await vc.disconnect(force=True)
    await message.reply("👋 Поплыл обратно в ананас!", mention_author=False)


VOICE_CMDS = {
    "!join":  (voice_join,  False),
    "!войти": (voice_join,  False),
    "!leave": (voice_leave, False),
    "!выйти": (voice_leave, False),
}


@bot.event
async def on_message(message: discord.Message):
    global is_bot_active, last_active_time

    if message.author.bot or message.author == bot.user:
        return

    content = message.content.strip()
    if not content:
        return

    parts   = content.split(maxsplit=1)
    cmd_key = parts[0].lower()

    if cmd_key in VOICE_CMDS:
        handler, _ = VOICE_CMDS[cmd_key]
        await handler(message)
        return

    # Управление через ЛС (команды # и %)
    if message.guild is None and message.author.id == OWNER_ID:
        if content.startswith("#"):
            text_to_say = content[1:].strip()
            vc = next((g.voice_client for g in bot.guilds if g.voice_client and g.voice_client.is_connected()), None)
            if not vc:
                await message.reply("❌ Бот не подключен к ГС.", mention_author=False)
                return
            audio = await synthesize(text_to_say)
            if audio:
                await play_in_vc(vc, audio)
                await message.reply(f"🔊 Озвучено в **{vc.channel.name}**", mention_author=False)
            return

        elif content.startswith("%"):
            question = content[1:].strip()
            vc = next((g.voice_client for g in bot.guilds if g.voice_client and g.voice_client.is_connected()), None)
            if not vc:
                await message.reply("❌ Бот не подключен к ГС.", mention_author=False)
                return
            reply = await gemini_voice(vc.channel.id, question, message.author.display_name)
            await send_to_owner_dm(f"🧽 **Губка Боб**: {reply}")
            audio = await synthesize(reply)
            if audio:
                await play_in_vc(vc, audio)
            return

    last_active_time = time.time()


@bot.event
async def on_ready():
    print(f"[BOT] Запущен как {bot.user} (ID: {bot.user.id})", flush=True)
    print(
        "[DIAGNOSTICS] "
        f"discord={getattr(discord, '__version__', 'unknown')} "
        f"discord.py-self={installed_version('discord.py-self')} "
        f"voice_recv={installed_version('discord-ext-voice-recv')} "
        f"davey={installed_version('davey')} "
        f"pynacl={installed_version('PyNaCl')} "
        f"stt_language={STT_LANGUAGE}",
        flush=True,
    )
    asyncio.create_task(start_keepalive())
    asyncio.create_task(self_ping_loop())
    asyncio.create_task(status_manager_loop())


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("[ОШИБКА] DISCORD_BOT_TOKEN не указан!")
    bot.run(DISCORD_BOT_TOKEN)