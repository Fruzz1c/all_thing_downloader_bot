# @all_thing_downloader_bot.py
# Универсальный Telegram-бот (Instagram/TikTok/YouTube/X/VK/Reddit и др.)
# - Показывает доступные разрешения, кнопки <= 64 байт (через короткий token).
# - Совместим с Python 3.14 (явно создаём event loop).
# - Увеличены таймауты HTTP-клиента для стабильной отправки файлов в Telegram.
# - Если ffmpeg не в PATH — можно указать FFMPEG_LOCATION.

from __future__ import annotations

import os
import re
import sys
import asyncio
import tempfile
import logging
import secrets
from pathlib import Path
from urllib.parse import urlparse

from yt_dlp import YoutubeDL
from telegram import (
    Update,
    InputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    Application,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.request import HTTPXRequest
from telegram.error import TimedOut

# ==============================
# 👉 Твой токен
import os
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")
# ==============================

# Если ffmpeg НЕ в PATH — пропиши путь к папке с ffmpeg/ffprobe:
FFMPEG_LOCATION: str | None = None  # Например: r"C:\ffmpeg\ffmpeg-2025-xx-xx-full_build\bin"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("downloader_bot")

BASE_YTDLP_OPTS: dict = {
    "outtmpl": "%(id)s.%(ext)s",
    "quiet": True,
    "no_warnings": True,
    "merge_output_format": "mp4",
    "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
}
if FFMPEG_LOCATION:
    BASE_YTDLP_OPTS["ffmpeg_location"] = FFMPEG_LOCATION

URL_RE = re.compile(r"(https?://[^\s]+)", re.IGNORECASE | re.MULTILINE)

# =================== Утилиты ===================

def extract_first_url(text: str) -> str | None:
    if not text:
        return None
    m = URL_RE.search(text)
    return m.group(1) if m else None

def hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""

def human_provider(url: str) -> str:
    host = hostname(url)
    if "instagram" in host:
        return "Instagram"
    if "tiktok" in host:
        return "TikTok"
    if host in {"youtu.be", "youtube.com", "www.youtube.com", "m.youtube.com"} or "youtube" in host:
        return "YouTube"
    if "twitter" in host or host == "x.com":
        return "X (Twitter)"
    if "vk.com" in host:
        return "VK"
    if "reddit" in host:
        return "Reddit"
    return host or "Источник"

def pick_filename_from_dir(target_dir: Path, media_id: str | None) -> Path | None:
    files = list(target_dir.iterdir())
    if not files:
        return None
    if media_id:
        for p in files:
            if media_id in p.name:
                return p
    return files[0]

def build_format_string(height: int | None) -> str:
    if not height:
        return "bestvideo*+bestaudio/best"
    return f"bestvideo*[height<=?{height}]+bestaudio/best[height<=?{height}]"

def unique_sorted_heights(formats: list[dict]) -> list[int]:
    heights = {f.get("height") for f in formats if isinstance(f.get("height"), int)}
    heights = [h for h in heights if h and h >= 240]
    heights.sort()
    return heights

async def ytdlp_extract(url: str, download: bool, fmt: str | None, target_dir: Path) -> dict | None:
    loop = asyncio.get_running_loop()

    def run_ydl():
        opts = dict(BASE_YTDLP_OPTS)
        if fmt:
            opts["format"] = fmt
        cwd = os.getcwd()
        try:
            os.chdir(target_dir)
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=download)
        finally:
            os.chdir(cwd)

    return await loop.run_in_executor(None, run_ydl)

# =================== Хранилище коротких callback ===================
# context.user_data["dl_store"] = { token: {"url": str, "tmpdir": TemporaryDirectory} }

def store_new_job(user_data: dict, url: str, tmpdir) -> str:
    store: dict = user_data.setdefault("dl_store", {})
    token = secrets.token_urlsafe(8)
    store[token] = {"url": url, "tmpdir": tmpdir}
    # простая эвикция
    if len(store) > 50:
        for k in list(store.keys())[: len(store) - 50]:
            job = store.pop(k, None)
            try:
                if job and "tmpdir" in job:
                    job["tmpdir"].cleanup()
            except Exception:
                pass
    return token

def pop_job(user_data: dict, token: str):
    store: dict = user_data.get("dl_store") or {}
    return store.pop(token, None)

# =================== Handlers ===================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Пришли ссылку на видео (Instagram, TikTok, YouTube, X/Twitter, VK, Reddit и др.).\n"
        "Покажу доступные разрешения — выберешь и получишь файл."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "1) Пришли ссылку на пост/видео из соцсети.\n"
        "2) Я предложу список доступных разрешений.\n"
        "3) Нажми нужное — скачаю и отправлю.\n\n"
        "Совет: поставь ffmpeg в PATH или укажи путь в FFMPEG_LOCATION в коде."
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = update.message.text or update.message.caption or ""
    url = extract_first_url(text)
    if not url:
        await update.message.reply_text("Пожалуйста, пришли ссылку на видео 🙏")
        return

    status = await update.message.reply_text("🔎 Анализирую ссылку…")

    tmpdir = tempfile.TemporaryDirectory()
    tmp_path = Path(tmpdir.name)

    try:
        info = await ytdlp_extract(url, download=False, fmt=None, target_dir=tmp_path)
        if not info:
            await status.edit_text("❌ Не удалось получить информацию по ссылке. Возможно, она недоступна.")
            tmpdir.cleanup()
            return

        if "entries" in info and isinstance(info["entries"], list) and info["entries"]:
            info = info["entries"][0]

        formats = info.get("formats") or []
        heights = unique_sorted_heights(formats)
        provider = human_provider(url)
        title = info.get("title") or "Видео"

        token = store_new_job(context.user_data, url, tmpdir)

        buttons: list[list[InlineKeyboardButton]] = []
        wanted = [240, 360, 480, 720, 1080, 1440, 2160]
        display_heights = [h for h in wanted if h in heights]
        if not display_heights:
            display_heights = heights[-3:] if len(heights) > 3 else heights

        row: list[InlineKeyboardButton] = []
        for h in display_heights:
            row.append(InlineKeyboardButton(text=f"{h}p", callback_data=f"dl|{token}|h{h}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton(text="Лучшее", callback_data=f"dl|{token}|best")])

        await status.edit_text(
            f"🌐 Источник: {provider}\n"
            f"📄 Название: {title}\n\n"
            f"Выбери разрешение:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception as e:
        logger.exception("Ошибка анализа")
        await status.edit_text(f"⚠️ Ошибка анализа ссылки: {e}")
        tmpdir.cleanup()

async def _send_with_retries_as_video_or_doc(message, file_path: Path, caption: str):
    """Сначала пытаемся как video, при неудаче — как документ. Оба с ретраями на TimedOut."""
    # показываем «печатаю» загрузку
    try:
        await message.chat.send_action(action=ChatAction.UPLOAD_VIDEO)
    except Exception:
        pass

    # 1) как видео
    for attempt in range(3):
        try:
            with file_path.open("rb") as f:
                return await message.reply_video(video=InputFile(f, filename=file_path.name), caption=caption)
        except TimedOut:
            if attempt == 2:
                break
            await asyncio.sleep(2 * (attempt + 1))
        except Exception:
            break  # падаем на документ

    # 2) как документ
    for attempt in range(3):
        try:
            with file_path.open("rb") as f:
                return await message.reply_document(document=InputFile(f, filename=file_path.name), caption=caption)
        except TimedOut:
            if attempt == 2:
                raise
            await asyncio.sleep(2 * (attempt + 1))

async def on_download_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = (query.data or "").split("|", 2)  # ["dl", token, "h720"/"best"]
    if len(data) != 3 or data[0] != "dl":
        await query.edit_message_text("Некорректные данные кнопки.")
        return

    token, mode = data[1], data[2]
    job = pop_job(context.user_data, token)
    if not job:
        await query.edit_message_text("⌛ Время выбора истекло. Пришли ссылку ещё раз.")
        return

    url = job.get("url")
    tmpdir = job.get("tmpdir")
    if not url or not tmpdir:
        await query.edit_message_text("Внутренняя ошибка: нет данных задачи.")
        try:
            if tmpdir:
                tmpdir.cleanup()
        except Exception:
            pass
        return

    chosen_height: int | None
    if mode == "best":
        chosen_height = None
    elif mode.startswith("h"):
        try:
            chosen_height = int(mode[1:])
        except ValueError:
            chosen_height = None
    else:
        chosen_height = None

    provider = human_provider(url)
    await query.edit_message_text(f"⬇️ Скачиваю из {provider}…")

    tmp_path = Path(tmpdir.name)

    try:
        fmt = build_format_string(chosen_height)
        info = await ytdlp_extract(url, download=True, fmt=fmt, target_dir=tmp_path)
        if not info:
            await query.edit_message_text("❌ Не удалось скачать видео (нет данных).")
            return

        if "entries" in info and isinstance(info["entries"], list) and info["entries"]:
            info = info["entries"][0]

        media_id = info.get("id") or info.get("display_id") or ""
        file_path = pick_filename_from_dir(tmp_path, media_id)
        if not file_path or not file_path.exists():
            await query.edit_message_text("❌ Не удалось найти скачанный файл. Возможно, источник ограничен.")
            return

        caption = (info.get("title") or "Видео")[:900]

        try:
            await _send_with_retries_as_video_or_doc(query.message, file_path, caption)
            await query.edit_message_text("✅ Готово!")
        except Exception as e_send:
            logger.exception("Ошибка отправки")
            await query.edit_message_text(f"⚠️ Не удалось отправить файл: {e_send}")

    except Exception as e:
        logger.exception("Ошибка скачивания/отправки")
        try:
            await query.edit_message_text(f"⚠️ Не удалось скачать/отправить: {e}")
        except Exception:
            pass
    finally:
        try:
            tmpdir.cleanup()
        except Exception:
            pass

# =================== Запуск (Python 3.14) ===================

def build_app() -> Application:
    # Большие таймауты, чтобы аплоад не упирался в write timeout
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=600.0,
        write_timeout=600.0,
        pool_timeout=30.0,
    )

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(request).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(on_download_choice, pattern=r"^dl\|"))
    return app

def main():
    logger.info("🚀 Запуск Telegram-бота...")

    # В Python 3.14 get_event_loop() не создаёт цикл автоматически — создаём явно
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
