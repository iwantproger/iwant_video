"""
VideoBot — Telegram бот для скачивания и отправки видео из YouTube, TikTok, Instagram и др.
"""

import os
import re
import uuid
import logging
import asyncio
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from telegram import (
    Update,
    InlineQueryResultVideo,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    InlineQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import TelegramError

# ─── Настройки ────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "your_bot_username")  # без @
BOT_LINK = f"https://t.me/{BOT_USERNAME}"

# Максимальный размер файла для Telegram Bot API (50 МБ)
MAX_FILE_SIZE_MB = 50
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# Качество видео: лучшее, умещающееся в лимит
VIDEO_FORMAT = "bestvideo[ext=mp4][filesize<45M]+bestaudio[ext=m4a]/best[ext=mp4][filesize<45M]/best[filesize<45M]/best"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Регулярки для определения ссылок ─────────────────────────────────────────
URL_PATTERN = re.compile(
    r"(https?://(?:www\.)?"
    r"(?:youtube\.com/watch\?[^\s]+|youtu\.be/[^\s]+"
    r"|instagram\.com/(?:p|reel|tv)/[^\s]+"
    r"|tiktok\.com/[^\s]+"
    r"|twitter\.com/[^\s]+/status/[^\s]+"
    r"|x\.com/[^\s]+/status/[^\s]+"
    r"|vimeo\.com/[^\s]+"
    r"|reddit\.com/r/[^\s]+/comments/[^\s]+"
    r"|twitch\.tv/[^\s]+"
    r"|dailymotion\.com/video/[^\s]+"
    r"|fb\.watch/[^\s]+"
    r"|facebook\.com/[^\s]+/videos/[^\s]+"
    r"|[^\s]+\.[a-z]{2,6}/[^\s]*))",
    re.IGNORECASE,
)

GENERIC_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def extract_url(text: str) -> str | None:
    """Извлекает первую видео-ссылку из текста."""
    match = URL_PATTERN.search(text)
    if match:
        return match.group(1)
    # Fallback — любая ссылка
    match = GENERIC_URL_PATTERN.search(text)
    return match.group(0) if match else None


# ─── Скачивание видео ──────────────────────────────────────────────────────────
def download_video(url: str, output_dir: str) -> dict | None:
    """
    Скачивает видео с помощью yt-dlp.
    Возвращает dict с ключами: path, title, duration, thumbnail
    """
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")

    ydl_opts = {
        "format": VIDEO_FORMAT,
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": MAX_FILE_SIZE_BYTES,
        "merge_output_format": "mp4",
        "postprocessors": [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }
        ],
        # Заголовки, чтобы не получить 403
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                return None

            # Определяем итоговый файл
            filename = ydl.prepare_filename(info)
            # После мержа расширение может смениться на .mp4
            if not Path(filename).exists():
                filename = str(Path(filename).with_suffix(".mp4"))
            if not Path(filename).exists():
                # Поищем любой скачанный файл в папке
                files = list(Path(output_dir).glob("*"))
                if not files:
                    return None
                filename = str(files[0])

            file_size = Path(filename).stat().st_size
            if file_size > MAX_FILE_SIZE_BYTES:
                logger.warning(f"Файл слишком большой: {file_size} байт")
                return None

            return {
                "path": filename,
                "title": info.get("title", "Видео"),
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
                "uploader": info.get("uploader", ""),
            }
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp DownloadError: {e}")
        return None
    except Exception as e:
        logger.error(f"Неожиданная ошибка при скачивании: {e}")
        return None


def build_caption(title: str, original_url: str) -> str:
    """Формирует подпись к видео."""
    return (
        f"🎬 <b>{title}</b>\n\n"
        f"🔗 <a href='{original_url}'>Оригинальное видео</a>\n"
        f"🤖 <a href='{BOT_LINK}'>@{BOT_USERNAME}</a>"
    )


# ─── Обработчики ──────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 <b>Привет! Я VideoBot.</b>\n\n"
        "Я умею скачивать видео из:\n"
        "▪️ YouTube\n"
        "▪️ TikTok\n"
        "▪️ Instagram\n"
        "▪️ Twitter / X\n"
        "▪️ Vimeo, Reddit, Twitch и многих других\n\n"
        "<b>Как использовать:</b>\n"
        "1️⃣ Отправь мне ссылку на видео — я скачаю и пришлю его.\n"
        f"2️⃣ В любом чате напиши <code>@{BOT_USERNAME} ссылка</code> — "
        "видео появится прямо в чате.\n\n"
        "Добавь меня в группу и я буду автоматически обрабатывать ссылки!"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📖 <b>Справка</b>\n\n"
        "<b>В личном чате:</b>\n"
        "Просто отправь ссылку на видео.\n\n"
        "<b>В группе:</b>\n"
        "Бот автоматически реагирует на ссылки из поддерживаемых сервисов.\n\n"
        "<b>Инлайн-режим:</b>\n"
        f"В любом чате напиши <code>@{BOT_USERNAME} https://...</code> "
        "и выбери результат — видео будет отправлено в чат.\n\n"
        "<b>Ограничения:</b>\n"
        "▪️ Максимальный размер видео — 50 МБ\n"
        "▪️ Для длинных видео может занять время\n\n"
        f"🤖 {BOT_LINK}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает входящие сообщения с ссылками."""
    message = update.message
    if not message or not message.text:
        return

    url = extract_url(message.text)
    if not url:
        # В группах молчим, в личке подсказываем
        if update.effective_chat.type == "private":
            await message.reply_text(
                "🔍 Не нашёл ссылку на видео. Отправь ссылку на YouTube, TikTok, Instagram и т.д."
            )
        return

    await process_and_send_video(update, context, url, reply_to=message.message_id)


async def process_and_send_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    reply_to: int | None = None,
) -> None:
    """Скачивает видео и отправляет его в чат."""
    chat_id = update.effective_chat.id

    # Индикатор загрузки
    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="⏳ Скачиваю видео, подожди немного...",
        reply_to_message_id=reply_to,
    )

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

    with tempfile.TemporaryDirectory() as tmpdir:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, download_video, url, tmpdir)

        if result is None:
            await status_msg.edit_text(
                "❌ Не удалось скачать видео.\n\n"
                "Возможные причины:\n"
                "▪️ Видео недоступно или удалено\n"
                "▪️ Видео больше 50 МБ\n"
                "▪️ Сервис временно недоступен\n\n"
                f"🔗 <a href='{url}'>Ссылка на видео</a>",
                parse_mode=ParseMode.HTML,
            )
            return

        caption = build_caption(result["title"], url)

        try:
            with open(result["path"], "rb") as video_file:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=video_file,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    duration=result.get("duration"),
                    supports_streaming=True,
                    reply_to_message_id=reply_to,
                )
            await status_msg.delete()
        except TelegramError as e:
            logger.error(f"Ошибка отправки видео: {e}")
            await status_msg.edit_text(
                f"❌ Видео скачано, но не удалось отправить: файл слишком большой.\n\n"
                f"🔗 <a href='{url}'>Смотри по ссылке</a>",
                parse_mode=ParseMode.HTML,
            )


# ─── Инлайн-режим ─────────────────────────────────────────────────────────────
async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает инлайн-запросы вида @bot https://..."""
    query = update.inline_query
    if not query:
        return

    text = query.query.strip()
    url = extract_url(text)

    if not url:
        # Показываем подсказку
        results = [
            InlineQueryResultArticle(
                id="hint",
                title="🎬 Скачать видео",
                description="Введи ссылку на YouTube, TikTok, Instagram...",
                input_message_content=InputTextMessageContent(
                    f"🤖 Используй @{BOT_USERNAME} + ссылка на видео\n\nПример:\n"
                    f"@{BOT_USERNAME} https://youtube.com/watch?v=..."
                ),
                thumbnail_url="https://i.imgur.com/4M34hi2.png",
            )
        ]
        await query.answer(results, cache_time=300)
        return

    # Показываем результат с кнопкой "Отправить"
    results = [
        InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title="🎬 Скачать и отправить видео",
            description=f"🔗 {url[:60]}{'...' if len(url) > 60 else ''}",
            input_message_content=InputTextMessageContent(
                f"⏳ Запрошено скачивание видео...\n\n🔗 {url}",
                parse_mode=ParseMode.HTML,
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🤖 Перейти к боту", url=BOT_LINK)]
            ]),
        )
    ]

    # Сохраняем URL для обработки через chosen_inline_result
    context.bot_data[f"inline_{query.id}"] = url

    await query.answer(
        results,
        cache_time=1,
        is_personal=True,
    )


async def chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Когда пользователь выбирает инлайн-результат, бот пытается отправить видео.
    Но из-за ограничений Telegram API (нельзя редактировать чужое сообщение с видео),
    мы отправляем видео в ЛС пользователю.
    """
    result = update.chosen_inline_result
    if not result:
        return

    url = context.bot_data.pop(f"inline_{result.inline_message_id}", None)
    if not url:
        # Попробуем достать из query
        url = extract_url(result.query)

    if not url:
        return

    user_id = result.from_user.id

    # Отправляем видео в ЛС
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"⏳ Скачиваю видео для тебя...\n🔗 {url}",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            loop = asyncio.get_event_loop()
            video_result = await loop.run_in_executor(None, download_video, url, tmpdir)

            if video_result:
                caption = build_caption(video_result["title"], url)
                with open(video_result["path"], "rb") as vf:
                    await context.bot.send_video(
                        chat_id=user_id,
                        video=vf,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        duration=video_result.get("duration"),
                        supports_streaming=True,
                    )
            else:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"❌ Не удалось скачать видео.\n🔗 {url}",
                )
    except TelegramError as e:
        logger.error(f"Ошибка в chosen_inline_result: {e}")


# ─── Запуск бота ──────────────────────────────────────────────────────────────
def main() -> None:
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise ValueError("Установи переменную окружения BOT_TOKEN!")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))

    # Сообщения с ссылками (в группах и личке)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Инлайн-режим
    app.add_handler(InlineQueryHandler(inline_query))

    logger.info("Бот запущен. Нажми Ctrl+C для остановки.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
