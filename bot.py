"""
Бот, Смотри прикол — Telegram бот для скачивания и отправки видео.
YouTube, TikTok, Instagram, Twitter/X, Vimeo, Reddit и 1000+ других сервисов.
"""

import os
import re
import uuid
import logging
import asyncio
import tempfile
import subprocess
from pathlib import Path

import yt_dlp
from telegram import (
    Update,
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
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import TelegramError

# ─── Настройки ────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "your_bot_username")  # без @
BOT_LINK     = f"https://t.me/{BOT_USERNAME}"

MAX_FILE_SIZE_MB    = 50
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

VIDEO_FORMAT = (
    "bestvideo[ext=mp4][filesize<45M]+bestaudio[ext=m4a]"
    "/best[ext=mp4][filesize<45M]"
    "/best[filesize<45M]"
    "/best"
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DEFAULT_PREFS = {"desc": True, "stats": True}

# ─── URL-паттерны ─────────────────────────────────────────────────────────────
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
    m = URL_PATTERN.search(text)
    if m:
        return m.group(1)
    m = GENERIC_URL_PATTERN.search(text)
    return m.group(0) if m else None


# ─── Утилиты ──────────────────────────────────────────────────────────────────
def strip_metadata(input_path: str, output_path: str) -> bool:
    """Убирает метаданные из файла — чтобы Telegram не показывал 'Video by Автор'."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-map_metadata", "-1",
             "-map", "0:v?", "-map", "0:a?",
             "-c", "copy", output_path],
            capture_output=True, timeout=120,
        )
        return result.returncode == 0
    except Exception as e:
        logger.error(f"strip_metadata error: {e}")
        return False


def format_number(n) -> str:
    if n is None:
        return "—"
    n = int(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


# ─── Скачивание ───────────────────────────────────────────────────────────────
def download_video(url: str, output_dir: str) -> dict | None:
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
    ydl_opts = {
        "format": VIDEO_FORMAT,
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": MAX_FILE_SIZE_BYTES,
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
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

            filename = ydl.prepare_filename(info)
            if not Path(filename).exists():
                filename = str(Path(filename).with_suffix(".mp4"))
            if not Path(filename).exists():
                files = list(Path(output_dir).glob("*"))
                if not files:
                    return None
                filename = str(files[0])

            # Убираем метаданные → нет плашки "Video by ..."
            clean_path = os.path.join(output_dir, "clean.mp4")
            if strip_metadata(filename, clean_path) and Path(clean_path).exists():
                filename = clean_path

            if Path(filename).stat().st_size > MAX_FILE_SIZE_BYTES:
                return None

            # Разрешение из info или из форматов
            width  = info.get("width")
            height = info.get("height")
            if not width or not height:
                for fmt in reversed(info.get("formats", [])):
                    if fmt.get("width") and fmt.get("height"):
                        width, height = fmt["width"], fmt["height"]
                        break

            description = (info.get("description") or "").strip()
            if len(description) > 800:
                description = description[:797] + "..."

            return {
                "path":          filename,
                "title":         info.get("title", "Видео"),
                "description":   description,
                "width":         width,
                "height":        height,
                "duration":      info.get("duration"),
                "view_count":    info.get("view_count"),
                "like_count":    info.get("like_count"),
                "comment_count": info.get("comment_count"),
                "uploader":      info.get("uploader") or info.get("channel") or "",
            }

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"DownloadError: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return None


# ─── Формирование подписи ─────────────────────────────────────────────────────
def build_stats_str(info: dict) -> str:
    parts = []
    if info.get("view_count") is not None:
        parts.append(f"👁 {format_number(info['view_count'])}")
    if info.get("like_count") is not None:
        parts.append(f"❤️ {format_number(info['like_count'])}")
    if info.get("comment_count") is not None:
        parts.append(f"💬 {format_number(info['comment_count'])}")
    return "  ".join(parts)


def build_caption(title, url, description, stats_str, show_desc, show_stats) -> str:
    parts = [f"🎬 <b>{title}</b>"]
    if show_stats and stats_str:
        parts.append(f"\n{stats_str}")
    if show_desc and description:
        parts.append(f"\n\n📝 {description}")
    parts.append(f"\n\n🔗 <a href='{url}'>Оригинал</a>  •  🤖 <a href='{BOT_LINK}'>@{BOT_USERNAME}</a>")
    return "".join(parts)


def make_toggle_keyboard(chat_id, msg_id, show_desc, show_stats) -> InlineKeyboardMarkup:
    d = "✅" if show_desc  else "☑️"
    s = "✅" if show_stats else "☑️"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{d} Описание",   callback_data=f"tog:{chat_id}:{msg_id}:desc"),
            InlineKeyboardButton(f"{s} Статистика", callback_data=f"tog:{chat_id}:{msg_id}:stats"),
        ],
        [
            InlineKeyboardButton("✅ Всё",           callback_data=f"tog:{chat_id}:{msg_id}:all"),
            InlineKeyboardButton("❌ Только ссылка", callback_data=f"tog:{chat_id}:{msg_id}:none"),
        ],
    ])


# ─── Отправка видео ────────────────────────────────────────────────────────────
async def process_and_send_video(update, context, url, reply_to=None):
    chat_id = update.effective_chat.id
    prefs = context.user_data.get("prefs", dict(DEFAULT_PREFS))

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="⏳ Скачиваю видео, подожди немного...",
        reply_to_message_id=reply_to,
    )
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

    with tempfile.TemporaryDirectory() as tmpdir:
        loop = asyncio.get_event_loop()
        r = await loop.run_in_executor(None, download_video, url, tmpdir)

        if r is None:
            await status_msg.edit_text(
                "❌ Не удалось скачать видео.\n\n"
                "▪️ Видео недоступно или удалено\n"
                "▪️ Файл больше 50 МБ\n"
                "▪️ Сервис временно недоступен\n\n"
                f"🔗 <a href='{url}'>Открыть по ссылке</a>",
                parse_mode=ParseMode.HTML,
            )
            return

        stats_str = build_stats_str(r)
        caption   = build_caption(r["title"], url, r["description"], stats_str, prefs["desc"], prefs["stats"])

        try:
            with open(r["path"], "rb") as vf:
                # Сначала отправляем с временной клавиатурой
                sent = await context.bot.send_video(
                    chat_id=chat_id, video=vf,
                    caption=caption, parse_mode=ParseMode.HTML,
                    duration=r.get("duration"),
                    width=r.get("width"),     # оригинальное разрешение
                    height=r.get("height"),   # оригинальное разрешение
                    supports_streaming=True,
                    reply_to_message_id=reply_to,
                )

            # Обновляем клавиатуру с правильным msg_id
            kb = make_toggle_keyboard(chat_id, sent.message_id, prefs["desc"], prefs["stats"])
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=sent.message_id, reply_markup=kb
            )

            # Сохраняем данные для переключения
            context.bot_data[f"vid:{chat_id}:{sent.message_id}"] = {
                "url": url, "title": r["title"], "description": r["description"],
                "stats_str": stats_str, "show_desc": prefs["desc"], "show_stats": prefs["stats"],
            }
            await status_msg.delete()

        except TelegramError as e:
            logger.error(f"Ошибка отправки: {e}")
            await status_msg.edit_text(
                "❌ Не удалось отправить (файл слишком большой).\n\n"
                f"🔗 <a href='{url}'>Смотри по ссылке</a>",
                parse_mode=ParseMode.HTML,
            )


# ─── Callback: кнопки под видео ───────────────────────────────────────────────
async def on_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    _, chat_id_str, msg_id_str, action = query.data.split(":", 3)
    chat_id, msg_id = int(chat_id_str), int(msg_id_str)

    key  = f"vid:{chat_id}:{msg_id}"
    data = context.bot_data.get(key)
    if not data:
        await query.answer("Данные устарели. Отправь ссылку заново.", show_alert=True)
        return

    sd, ss = data["show_desc"], data["show_stats"]
    if action == "desc":
        sd = not sd
    elif action == "stats":
        ss = not ss
    elif action == "all":
        sd, ss = True, True
    elif action == "none":
        sd, ss = False, False

    data["show_desc"], data["show_stats"] = sd, ss
    context.bot_data[key] = data

    await query.edit_message_caption(
        caption=build_caption(data["title"], data["url"], data["description"], data["stats_str"], sd, ss),
        parse_mode=ParseMode.HTML,
        reply_markup=make_toggle_keyboard(chat_id, msg_id, sd, ss),
    )


# ─── Callback: /settings ──────────────────────────────────────────────────────
async def on_pref_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]

    prefs = context.user_data.get("prefs", dict(DEFAULT_PREFS))
    if   action == "desc":  prefs["desc"]  = not prefs["desc"]
    elif action == "stats": prefs["stats"] = not prefs["stats"]
    elif action == "all":   prefs["desc"] = prefs["stats"] = True
    elif action == "none":  prefs["desc"] = prefs["stats"] = False
    context.user_data["prefs"] = prefs

    d = "✅" if prefs["desc"]  else "☑️"
    s = "✅" if prefs["stats"] else "☑️"
    await query.edit_message_text(
        f"⚙️ <b>Настройки по умолчанию</b>\n\n{d} Описание  {s} Статистика\n\nПрименятся к следующим видео.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{d} Описание", callback_data="pref:desc"),
             InlineKeyboardButton(f"{s} Статистика", callback_data="pref:stats")],
            [InlineKeyboardButton("✅ Включить всё", callback_data="pref:all"),
             InlineKeyboardButton("❌ Выключить всё", callback_data="pref:none")],
        ]),
    )


# ─── Команды ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 <b>Привет! Я — Бот, Смотри прикол 🎬</b>\n\n"
        "Скидывай ссылки на видео — скачаю и пришлю прямо в чат.\n\n"
        "Поддерживаю:\n"
        "▪️ YouTube / Shorts\n"
        "▪️ TikTok\n"
        "▪️ Instagram Reels\n"
        "▪️ Twitter / X\n"
        "▪️ Vimeo, Reddit, Twitch и ещё 1000+ сайтов\n\n"
        "<b>Как использовать:</b>\n"
        "1️⃣ Отправь ссылку — получи видео\n"
        f"2️⃣ <code>@{BOT_USERNAME} ссылка</code> — в любом чате\n"
        "3️⃣ Добавь меня в группу — работаю там тоже\n\n"
        "Под каждым видео — кнопки: включай/выключай описание и статистику 👇\n\n"
        "⚙️ /settings — настройки  |  ❓ /help",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prefs = context.user_data.get("prefs", dict(DEFAULT_PREFS))
    d = "✅" if prefs["desc"] else "☑️"
    s = "✅" if prefs["stats"] else "☑️"
    await update.message.reply_text(
        "📖 <b>Справка</b>\n\n"
        "<b>Личный чат:</b> отправь ссылку\n"
        "<b>Группа:</b> добавь бота, он реагирует на ссылки\n"
        f"<b>Инлайн:</b> <code>@{BOT_USERNAME} ссылка</code>\n\n"
        "<b>Кнопки под видео:</b>\n"
        "✅/☑️ Описание — текст из оригинала\n"
        "✅/☑️ Статистика — просмотры, лайки, комменты\n\n"
        f"<b>Твои настройки:</b> {d} Описание  {s} Статистика\n\n"
        "Лимит: 50 МБ (ограничение Telegram)\n\n"
        f"⚙️ /settings\n🤖 {BOT_LINK}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prefs = context.user_data.get("prefs", dict(DEFAULT_PREFS))
    d = "✅" if prefs["desc"] else "☑️"
    s = "✅" if prefs["stats"] else "☑️"
    await update.message.reply_text(
        f"⚙️ <b>Настройки по умолчанию</b>\n\n{d} Описание  {s} Статистика",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{d} Описание", callback_data="pref:desc"),
             InlineKeyboardButton(f"{s} Статистика", callback_data="pref:stats")],
            [InlineKeyboardButton("✅ Включить всё", callback_data="pref:all"),
             InlineKeyboardButton("❌ Выключить всё", callback_data="pref:none")],
        ]),
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.text:
        return
    url = extract_url(msg.text)
    if not url:
        if update.effective_chat.type == "private":
            await msg.reply_text("🔍 Не нашёл ссылку. Отправь ссылку на YouTube, TikTok, Instagram и т.д.\n\n❓ /help")
        return
    await process_and_send_video(update, context, url, reply_to=msg.message_id)


# ─── Инлайн ───────────────────────────────────────────────────────────────────
async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query
    url = extract_url(query.query.strip()) if query else None
    if not url:
        await query.answer([InlineQueryResultArticle(
            id="hint", title="🎬 Скачать видео",
            description="Введи ссылку на YouTube, TikTok, Instagram...",
            input_message_content=InputTextMessageContent(
                f"🤖 <a href='{BOT_LINK}'>Бот, Смотри прикол</a>", parse_mode=ParseMode.HTML),
        )], cache_time=300)
        return

    context.bot_data[f"inline_{query.id}"] = url
    await query.answer([InlineQueryResultArticle(
        id=str(uuid.uuid4()),
        title="🎬 Скачать и отправить видео",
        description=url[:70] + ("..." if len(url) > 70 else ""),
        input_message_content=InputTextMessageContent(
            f"⏳ Запрошено видео...\n🔗 {url}\n🤖 <a href='{BOT_LINK}'>@{BOT_USERNAME}</a>",
            parse_mode=ParseMode.HTML),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Открыть бота", url=BOT_LINK)]]),
    )], cache_time=1, is_personal=True)


async def chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = update.chosen_inline_result
    if not result:
        return
    url = context.bot_data.pop(f"inline_{result.inline_message_id}", None) or extract_url(result.query)
    if not url:
        return
    user_id = result.from_user.id
    prefs = context.user_data.get("prefs", dict(DEFAULT_PREFS))
    try:
        await context.bot.send_message(chat_id=user_id, text=f"⏳ Скачиваю...\n🔗 {url}")
        with tempfile.TemporaryDirectory() as tmpdir:
            r = await asyncio.get_event_loop().run_in_executor(None, download_video, url, tmpdir)
            if r:
                ss = build_stats_str(r)
                cap = build_caption(r["title"], url, r["description"], ss, prefs["desc"], prefs["stats"])
                with open(r["path"], "rb") as vf:
                    sent = await context.bot.send_video(
                        chat_id=user_id, video=vf, caption=cap, parse_mode=ParseMode.HTML,
                        duration=r.get("duration"), width=r.get("width"), height=r.get("height"),
                        supports_streaming=True,
                    )
                kb = make_toggle_keyboard(user_id, sent.message_id, prefs["desc"], prefs["stats"])
                await context.bot.edit_message_reply_markup(chat_id=user_id, message_id=sent.message_id, reply_markup=kb)
                context.bot_data[f"vid:{user_id}:{sent.message_id}"] = {
                    "url": url, "title": r["title"], "description": r["description"],
                    "stats_str": ss, "show_desc": prefs["desc"], "show_stats": prefs["stats"],
                }
            else:
                await context.bot.send_message(chat_id=user_id, text=f"❌ Не удалось скачать.\n🔗 {url}")
    except TelegramError as e:
        logger.error(f"chosen_inline_result error: {e}")


# ─── Запуск ───────────────────────────────────────────────────────────────────
def main() -> None:
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise ValueError("Установи BOT_TOKEN!")
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CallbackQueryHandler(on_toggle_callback, pattern=r"^tog:"))
    app.add_handler(CallbackQueryHandler(on_pref_callback,   pattern=r"^pref:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(InlineQueryHandler(inline_query))
    logger.info("🎬 Бот, Смотри прикол — запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
