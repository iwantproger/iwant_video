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
import threading
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
BOT_USERNAME = os.environ.get("BOT_USERNAME", "your_bot_username")
BOT_LINK     = f"https://t.me/{BOT_USERNAME}"

MAX_FILE_SIZE_MB    = 50
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

VIDEO_FORMAT = (
    "bestvideo[vcodec^=avc][ext=mp4][filesize<45M]+bestaudio[ext=m4a]"
    "/bestvideo[vcodec^=avc][filesize<45M]+bestaudio"
    "/bestvideo[ext=mp4][filesize<45M]+bestaudio[ext=m4a]"
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

# ─── URL-утилиты ──────────────────────────────────────────────────────────────
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


def is_kk_platform(url: str) -> bool:
    """Instagram или TikTok — платформы с kk-fallback."""
    return bool(re.search(r"(instagram\.com|tiktok\.com)", url, re.IGNORECASE))


def is_youtube(url: str) -> bool:
    return bool(re.search(r"(youtube\.com|youtu\.be)", url, re.IGNORECASE))


def to_kk_url(url: str) -> str:
    """
    Заменяет 'www.' на 'kk' в URL.
    Если 'www.' нет — добавляет 'kk' перед доменом.

    Примеры:
      https://www.instagram.com/reel/xxx  →  https://kkinstagram.com/reel/xxx
      https://instagram.com/p/xxx         →  https://kkinstagram.com/p/xxx
      https://www.tiktok.com/@u/video/1   →  https://kktiktok.com/@u/video/1
      https://tiktok.com/@u/video/1       →  https://kktiktok.com/@u/video/1
    """
    # Убираем www. и добавляем kk перед доменом
    url = re.sub(r"(?i)https?://www\.", "https://kk", url)
    # Если www. не было — добавляем kk после схемы
    url = re.sub(r"(?i)(https?://)(?!kk)", r"\1kk", url)
    return url


# ─── FFmpeg-обработка ─────────────────────────────────────────────────────────
def strip_metadata(input_path: str, output_path: str, reencode: bool = False) -> bool:
    """
    Обрабатывает видео для Telegram.

    reencode=False (TikTok, Instagram и др.) — быстро:
        просто убирает метаданные и переставляет moov в начало, потоки копируются.

    reencode=True (YouTube) — медленнее, зато надёжно:
        перекодирует в yuv420p/libx264 — исправляет белый экран и стопкадр
        у Shorts и других роликов с нестандартным pixel format.
    """
    try:
        if reencode:
            cmd = [
                "ffmpeg", "-y",
                "-i", input_path,
                "-map_metadata", "-1",
                "-map", "0:v?",
                "-map", "0:a?",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-profile:v", "high",
                "-level:v", "4.1",
                "-c:a", "copy",
                "-movflags", "+faststart",
                output_path,
            ]
            timeout = 300
        else:
            cmd = [
                "ffmpeg", "-y",
                "-i", input_path,
                "-map_metadata", "-1",
                "-map", "0:v?",
                "-map", "0:a?",
                "-c", "copy",
                "-movflags", "+faststart",
                output_path,
            ]
            timeout = 60

        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            logger.error(f"ffmpeg stderr: {result.stderr.decode()[-500:]}")
        return result.returncode == 0
    except Exception as e:
        logger.error(f"strip_metadata error: {e}")
        return False


def format_number(n) -> str:
    if n is None:
        return "—"
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


# ─── Скачивание ───────────────────────────────────────────────────────────────
class DownloadCancelled(Exception):
    pass


def download_video(url: str, output_dir: str, cancel_event: threading.Event | None = None, reencode: bool = False) -> dict | None:
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")

    def progress_hook(d):
        """Вызывается yt-dlp во время загрузки. Бросает исключение при отмене."""
        if cancel_event and cancel_event.is_set():
            raise DownloadCancelled("Отменено пользователем")

    ydl_opts = {
        "format": VIDEO_FORMAT,
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": MAX_FILE_SIZE_BYTES,
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
        "progress_hooks": [progress_hook],
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

            clean_path = os.path.join(output_dir, "clean.mp4")
            if strip_metadata(filename, clean_path, reencode=reencode) and Path(clean_path).exists():
                filename = clean_path

            if Path(filename).stat().st_size > MAX_FILE_SIZE_BYTES:
                return None

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

    except DownloadCancelled:
        logger.info(f"Скачивание отменено: {url}")
        return None
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


def build_caption(
    url: str,
    description: str,
    stats_str: str,
    show_desc: bool,
    show_stats: bool,
    sender_name: str = "",      # имя пользователя, который прислал ссылку
) -> str:
    parts = []

    # Кто прислал (только в группах)
    if sender_name:
        parts.append(f"👤 <b>{sender_name}</b>")

    if show_stats and stats_str:
        sep = "\n" if parts else ""
        parts.append(f"{sep}{stats_str}")

    if show_desc and description:
        sep = "\n\n" if parts else ""
        parts.append(f"{sep}📝 {description}")

    link_line = f"🔗 <a href='{url}'>Оригинал</a>  •  🤖 <a href='{BOT_LINK}'>@{BOT_USERNAME}</a>"
    if parts:
        parts.append(f"\n\n{link_line}")
    else:
        parts.append(link_line)

    return "".join(parts)


# ─── Клавиатуры ───────────────────────────────────────────────────────────────
def make_main_keyboard(chat_id: int, msg_id: int, is_kk: bool) -> InlineKeyboardMarkup:
    """
    Для Instagram/TikTok: [Через Бот ✅] [Через kk]  +  [Доп.инфа]
    Для остальных: сразу кнопки desc/stats
    """
    cid, mid = chat_id, msg_id
    if is_kk:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🤖 Через Бот ✅", callback_data=f"bot_active:{cid}:{mid}"),
                InlineKeyboardButton("🔗 Через kk",     callback_data=f"send_kk:{cid}:{mid}"),
            ],
            [
                InlineKeyboardButton("ℹ️ Доп.инфа", callback_data=f"info:{cid}:{mid}"),
            ],
        ])
    else:
        return make_toggle_keyboard(chat_id, msg_id, show_desc=True, show_stats=True, back=False)


def make_toggle_keyboard(
    chat_id: int,
    msg_id: int,
    show_desc: bool,
    show_stats: bool,
    back: bool = False,          # True — показывать кнопку «← Назад»
) -> InlineKeyboardMarkup:
    d = "✅" if show_desc  else "☑️"
    s = "✅" if show_stats else "☑️"
    rows = [
        [
            InlineKeyboardButton(f"{d} Описание",   callback_data=f"tog:{chat_id}:{msg_id}:desc"),
            InlineKeyboardButton(f"{s} Статистика", callback_data=f"tog:{chat_id}:{msg_id}:stats"),
        ],
        [
            InlineKeyboardButton("✅ Всё",           callback_data=f"tog:{chat_id}:{msg_id}:all"),
            InlineKeyboardButton("❌ Только ссылка", callback_data=f"tog:{chat_id}:{msg_id}:none"),
        ],
    ]
    if back:
        rows.append([InlineKeyboardButton("← Назад", callback_data=f"back:{chat_id}:{msg_id}")])
    return InlineKeyboardMarkup(rows)


def make_cancel_keyboard(chat_id: int, status_msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🚫 Отмена", callback_data=f"cancel:{chat_id}:{status_msg_id}"),
    ]])


# ─── Отправка видео ────────────────────────────────────────────────────────────
async def process_and_send_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    reply_to: int | None = None,
    sender_name: str = "",
) -> None:
    chat_id = update.effective_chat.id
    prefs   = context.user_data.get("prefs", dict(DEFAULT_PREFS))
    kk      = is_kk_platform(url)
    reencode = is_youtube(url)   # перекодируем только YouTube

    # Отправляем статусное сообщение с кнопкой Отмена
    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="⏳ Скачиваю видео, подожди немного...",
        reply_to_message_id=reply_to,
        reply_markup=make_cancel_keyboard(chat_id, 0),  # временный id, обновим ниже
    )
    # Обновляем клавиатуру с реальным msg_id статусного сообщения
    await context.bot.edit_message_reply_markup(
        chat_id=chat_id,
        message_id=status_msg.message_id,
        reply_markup=make_cancel_keyboard(chat_id, status_msg.message_id),
    )
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

    # Создаём событие отмены и сохраняем в bot_data
    cancel_event = threading.Event()
    cancel_key   = f"cancel:{chat_id}:{status_msg.message_id}"
    context.bot_data[cancel_key] = cancel_event

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            loop = asyncio.get_event_loop()
            r = await loop.run_in_executor(
                None, download_video, url, tmpdir, cancel_event, reencode
            )

            # Если пользователь нажал Отмена — событие уже обработано в on_callback,
            # статусное сообщение уже удалено; просто выходим тихо.
            if cancel_event.is_set():
                return

            # ── Instagram/TikTok fallback через kk ──────────────────────────
            if r is None and kk:
                kk_url = to_kk_url(url)
                await status_msg.edit_text(
                    "⚠️ Не удалось скачать напрямую.\n"
                    "⏳ Пробую через kk...",
                    reply_markup=make_cancel_keyboard(chat_id, status_msg.message_id),
                )
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"🔗 <a href='{kk_url}'>{kk_url}</a>\n\n"
                            f"🤖 <a href='{BOT_LINK}'>@{BOT_USERNAME}</a>"
                        ),
                        parse_mode=ParseMode.HTML,
                        reply_to_message_id=reply_to,
                        disable_web_page_preview=False,
                    )
                    await status_msg.delete()
                    return
                except TelegramError as e:
                    logger.error(f"kk fallback error: {e}")

            if r is None:
                await status_msg.edit_text(
                    "❌ Не удалось скачать видео.\n\n"
                    "▪️ Видео недоступно или удалено\n"
                    "▪️ Файл больше 50 МБ\n"
                    "▪️ Сервис временно недоступен\n\n"
                    f"🔗 <a href='{url}'>Открыть по ссылке</a>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                )
                return

            stats_str = build_stats_str(r)
            caption   = build_caption(
                url=url,
                description=r["description"],
                stats_str=stats_str,
                show_desc=prefs["desc"],
                show_stats=prefs["stats"],
                sender_name=sender_name,
            )

            try:
                # Обновляем статус перед загрузкой файла
                await status_msg.edit_text(
                    "📤 Отправляю видео...",
                    reply_markup=make_cancel_keyboard(chat_id, status_msg.message_id),
                )
                with open(r["path"], "rb") as vf:
                    sent = await context.bot.send_video(
                        chat_id=chat_id,
                        video=vf,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        duration=r.get("duration"),
                        width=r.get("width"),
                        height=r.get("height"),
                        supports_streaming=True,
                        reply_to_message_id=reply_to,
                    )

                kb = make_main_keyboard(chat_id, sent.message_id, is_kk=kk)
                await context.bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=sent.message_id, reply_markup=kb
                )
                context.bot_data[f"vid:{chat_id}:{sent.message_id}"] = {
                    "url":         url,
                    "kk_url":      to_kk_url(url) if kk else "",
                    "is_kk":       kk,
                    "title":       r["title"],
                    "description": r["description"],
                    "stats_str":   stats_str,
                    "show_desc":   prefs["desc"],
                    "show_stats":  prefs["stats"],
                    "sender_name": sender_name,
                }
                await status_msg.delete()

            except TelegramError as e:
                logger.error(f"Ошибка отправки: {e}")
                await status_msg.edit_text(
                    "❌ Не удалось отправить (файл слишком большой).\n\n"
                    f"🔗 <a href='{url}'>Смотри по ссылке</a>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                )
    finally:
        # Всегда чистим ключ отмены
        context.bot_data.pop(cancel_key, None)


# ─── Callbacks ────────────────────────────────────────────────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Единый обработчик всех callback-кнопок под видео."""
    query  = update.callback_query
    await query.answer()
    parts  = query.data.split(":")
    action = parts[0]

    # ── Отмена скачивания ────────────────────────────────────────────────────
    if action == "cancel":
        if len(parts) < 3:
            return
        c_chat_id   = int(parts[1])
        c_status_id = int(parts[2])
        cancel_key  = f"cancel:{c_chat_id}:{c_status_id}"

        cancel_event: threading.Event | None = context.bot_data.pop(cancel_key, None)
        if cancel_event:
            cancel_event.set()   # сигнализируем потоку остановиться

        # Удаляем статусное сообщение (оно же несёт кнопку «Отмена»)
        try:
            await context.bot.delete_message(
                chat_id=c_chat_id, message_id=c_status_id
            )
        except TelegramError:
            pass   # уже удалено или недоступно

        await query.answer("🚫 Отменено", show_alert=False)
        return

    # ── /settings кнопки (pref:*) ────────────────────────────────────────────
    if action == "pref":
        sub    = parts[1]
        prefs  = context.user_data.get("prefs", dict(DEFAULT_PREFS))
        if   sub == "desc":  prefs["desc"]  = not prefs["desc"]
        elif sub == "stats": prefs["stats"] = not prefs["stats"]
        elif sub == "all":   prefs["desc"] = prefs["stats"] = True
        elif sub == "none":  prefs["desc"] = prefs["stats"] = False
        context.user_data["prefs"] = prefs
        d = "✅" if prefs["desc"]  else "☑️"
        s = "✅" if prefs["stats"] else "☑️"
        await query.edit_message_text(
            f"⚙️ <b>Настройки по умолчанию</b>\n\n{d} Описание  {s} Статистика\n\nПрименятся к следующим видео.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{d} Описание",    callback_data="pref:desc"),
                 InlineKeyboardButton(f"{s} Статистика",  callback_data="pref:stats")],
                [InlineKeyboardButton("✅ Включить всё",  callback_data="pref:all"),
                 InlineKeyboardButton("❌ Выключить всё", callback_data="pref:none")],
            ]),
        )
        return

    # ── Все остальные кнопки требуют chat_id и msg_id ────────────────────────
    if len(parts) < 3:
        return
    chat_id = int(parts[1])
    msg_id  = int(parts[2])
    key     = f"vid:{chat_id}:{msg_id}"
    data    = context.bot_data.get(key)

    if not data:
        await query.answer("Данные устарели. Отправь ссылку заново.", show_alert=True)
        return

    # ── "Через Бот" (уже активно, просто алерт) ──────────────────────────────
    if action == "bot_active":
        await query.answer("Видео уже отправлено через бота ✅", show_alert=False)
        return

    # ── "Через kk" — отправляем kk-ссылку отдельным сообщением ──────────────
    if action == "send_kk":
        kk_url = data.get("kk_url") or to_kk_url(data["url"])
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🔗 <a href='{kk_url}'>{kk_url}</a>\n\n"
                    f"🤖 <a href='{BOT_LINK}'>@{BOT_USERNAME}</a>"
                ),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
            # Меняем кнопку на "активную kk"
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("🤖 Через Бот",    callback_data=f"bot_active:{chat_id}:{msg_id}"),
                        InlineKeyboardButton("🔗 Через kk ✅",  callback_data=f"kk_active:{chat_id}:{msg_id}"),
                    ],
                    [InlineKeyboardButton("ℹ️ Доп.инфа", callback_data=f"info:{chat_id}:{msg_id}")],
                ])
            )
        except TelegramError as e:
            logger.error(f"send_kk error: {e}")
        return

    # ── "kk уже активно" — просто алерт ──────────────────────────────────────
    if action == "kk_active":
        await query.answer("kk-ссылка уже отправлена ✅", show_alert=False)
        return

    # ── "Доп.инфа" — показываем toggle-клавиатуру с кнопкой Назад ───────────
    if action == "info":
        sd, ss = data["show_desc"], data["show_stats"]
        await query.edit_message_reply_markup(
            reply_markup=make_toggle_keyboard(chat_id, msg_id, sd, ss, back=True)
        )
        return

    # ── "← Назад" — возвращаемся к основной клавиатуре ───────────────────────
    if action == "back":
        await query.edit_message_reply_markup(
            reply_markup=make_main_keyboard(chat_id, msg_id, is_kk=data["is_kk"])
        )
        return

    # ── Переключение описания / статистики (tog:*) ────────────────────────────
    if action == "tog":
        sub        = parts[3] if len(parts) > 3 else ""
        sd, ss     = data["show_desc"], data["show_stats"]
        if   sub == "desc":  sd = not sd
        elif sub == "stats": ss = not ss
        elif sub == "all":   sd, ss = True, True
        elif sub == "none":  sd, ss = False, False

        data["show_desc"], data["show_stats"] = sd, ss
        context.bot_data[key] = data

        new_caption = build_caption(
            url=data["url"],
            description=data["description"],
            stats_str=data["stats_str"],
            show_desc=sd,
            show_stats=ss,
            sender_name=data.get("sender_name", ""),
        )
        # Определяем, какую клавиатуру показывать (toggle, если уже в режиме info)
        # Остаёмся в toggle-режиме с кнопкой Назад (если есть is_kk)
        kb = make_toggle_keyboard(chat_id, msg_id, sd, ss, back=data["is_kk"])
        await query.edit_message_caption(
            caption=new_caption,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return


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
        "⚙️ /settings — настройки  |  ❓ /help",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prefs = context.user_data.get("prefs", dict(DEFAULT_PREFS))
    d = "✅" if prefs["desc"]  else "☑️"
    s = "✅" if prefs["stats"] else "☑️"
    await update.message.reply_text(
        "📖 <b>Справка</b>\n\n"
        "<b>Личный чат:</b> отправь ссылку\n"
        "<b>Группа:</b> добавь бота, он реагирует на ссылки\n"
        f"<b>Инлайн:</b> <code>@{BOT_USERNAME} ссылка</code>\n\n"
        "<b>Кнопки под видео из Instagram / TikTok:</b>\n"
        "🤖 Через Бот — скачанное видео\n"
        "🔗 Через kk — ссылка через kk-зеркало\n"
        "ℹ️ Доп.инфа — описание и статистика\n\n"
        "<b>Кнопки под остальными видео:</b>\n"
        f"{d} Описание  {s} Статистика\n\n"
        "Лимит: 50 МБ (ограничение Telegram)\n\n"
        f"⚙️ /settings\n🤖 {BOT_LINK}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prefs = context.user_data.get("prefs", dict(DEFAULT_PREFS))
    d = "✅" if prefs["desc"]  else "☑️"
    s = "✅" if prefs["stats"] else "☑️"
    await update.message.reply_text(
        f"⚙️ <b>Настройки по умолчанию</b>\n\n{d} Описание  {s} Статистика",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{d} Описание",    callback_data="pref:desc"),
             InlineKeyboardButton(f"{s} Статистика",  callback_data="pref:stats")],
            [InlineKeyboardButton("✅ Включить всё",  callback_data="pref:all"),
             InlineKeyboardButton("❌ Выключить всё", callback_data="pref:none")],
        ]),
    )


# ─── Обработчик сообщений ─────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.text:
        return

    url = extract_url(msg.text)
    if not url:
        if update.effective_chat.type == "private":
            await msg.reply_text(
                "🔍 Не нашёл ссылку.\n"
                "Отправь ссылку на YouTube, TikTok, Instagram и т.д.\n\n❓ /help"
            )
        return

    # Определяем имя отправителя (показываем только в группах)
    sender_name = ""
    if update.effective_chat.type in ("group", "supergroup"):
        user = msg.from_user
        if user:
            sender_name = user.full_name or user.first_name or ""

    await process_and_send_video(
        update, context, url,
        reply_to=msg.message_id,
        sender_name=sender_name,
    )


# ─── Инлайн ───────────────────────────────────────────────────────────────────
async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query
    url   = extract_url(query.query.strip()) if query else None
    if not url:
        await query.answer([InlineQueryResultArticle(
            id="hint",
            title="🎬 Скачать видео",
            description="Введи ссылку на YouTube, TikTok, Instagram...",
            input_message_content=InputTextMessageContent(
                f"🤖 <a href='{BOT_LINK}'>Бот, Смотри прикол</a>",
                parse_mode=ParseMode.HTML,
            ),
        )], cache_time=300)
        return

    context.bot_data[f"inline_{query.id}"] = url
    await query.answer([InlineQueryResultArticle(
        id=str(uuid.uuid4()),
        title="🎬 Скачать и отправить видео",
        description=url[:70] + ("..." if len(url) > 70 else ""),
        input_message_content=InputTextMessageContent(
            f"⏳ Запрошено видео...\n🔗 {url}\n🤖 <a href='{BOT_LINK}'>@{BOT_USERNAME}</a>",
            parse_mode=ParseMode.HTML,
        ),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🎬 Открыть бота", url=BOT_LINK)
        ]]),
    )], cache_time=1, is_personal=True)


async def chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = update.chosen_inline_result
    if not result:
        return
    url = (
        context.bot_data.pop(f"inline_{result.inline_message_id}", None)
        or extract_url(result.query)
    )
    if not url:
        return

    user_id = result.from_user.id
    prefs   = context.user_data.get("prefs", dict(DEFAULT_PREFS))
    kk      = is_kk_platform(url)

    try:
        await context.bot.send_message(chat_id=user_id, text=f"⏳ Скачиваю...\n🔗 {url}")
        with tempfile.TemporaryDirectory() as tmpdir:
            r = await asyncio.get_event_loop().run_in_executor(None, download_video, url, tmpdir)

            if r:
                stats_str = build_stats_str(r)
                caption   = build_caption(
                    url=url,
                    description=r["description"],
                    stats_str=stats_str,
                    show_desc=prefs["desc"],
                    show_stats=prefs["stats"],
                )
                with open(r["path"], "rb") as vf:
                    sent = await context.bot.send_video(
                        chat_id=user_id, video=vf,
                        caption=caption, parse_mode=ParseMode.HTML,
                        duration=r.get("duration"),
                        width=r.get("width"), height=r.get("height"),
                        supports_streaming=True,
                    )
                kb = make_main_keyboard(user_id, sent.message_id, is_kk=kk)
                await context.bot.edit_message_reply_markup(
                    chat_id=user_id, message_id=sent.message_id, reply_markup=kb
                )
                context.bot_data[f"vid:{user_id}:{sent.message_id}"] = {
                    "url":         url,
                    "kk_url":      to_kk_url(url) if kk else "",
                    "is_kk":       kk,
                    "title":       r["title"],
                    "description": r["description"],
                    "stats_str":   stats_str,
                    "show_desc":   prefs["desc"],
                    "show_stats":  prefs["stats"],
                    "sender_name": "",
                }
            else:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"❌ Не удалось скачать.\n🔗 {url}",
                )
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

    # Все callback-кнопки — один обработчик
    app.add_handler(CallbackQueryHandler(on_callback))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(InlineQueryHandler(inline_query))

    logger.info("🎬 Бот, Смотри прикол — запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
