"""
Бот, Смотри прикол — Telegram бот для скачивания и отправки видео.
YouTube, TikTok, Instagram, Twitter/X, Vimeo, Reddit и 1000+ других сервисов.
"""

import os
import re
import time
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

# Для YouTube — предпочитаем H.264 (avc), чтобы минимизировать перекодировку
YT_FORMAT = (
    "bestvideo[vcodec^=avc][ext=mp4][filesize<45M]+bestaudio[ext=m4a]"
    "/bestvideo[vcodec^=avc][filesize<45M]+bestaudio"
    "/bestvideo[ext=mp4][filesize<45M]+bestaudio[ext=m4a]"
    "/best[ext=mp4][filesize<45M]/best[filesize<45M]/best"
)
# Для остальных — лучшее качество без ограничения по кодеку
DEFAULT_FORMAT = (
    "bestvideo[ext=mp4][filesize<45M]+bestaudio[ext=m4a]"
    "/best[ext=mp4][filesize<45M]/best[filesize<45M]/best"
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


def is_youtube(url: str) -> bool:
    return bool(re.search(r"(youtube\.com|youtu\.be)", url, re.IGNORECASE))


def is_kk_platform(url: str) -> bool:
    """Instagram или TikTok — платформы с kk-переключением."""
    return bool(re.search(r"(instagram\.com|tiktok\.com)", url, re.IGNORECASE))


def to_kk_url(url: str) -> str:
    """
    Строит kk-зеркало URL.

    TikTok: всё между https:// и tiktok.com удаляется, заменяется на kk
      https://vm.tiktok.com/xxx     → https://kktiktok.com/xxx
      https://www.tiktok.com/xxx    → https://kktiktok.com/xxx
      https://tiktok.com/xxx        → https://kktiktok.com/xxx

    Instagram: убирается www., добавляется kk
      https://www.instagram.com/xxx → https://kkinstagram.com/xxx
      https://instagram.com/xxx     → https://kkinstagram.com/xxx
    """
    if re.search(r"tiktok\.com", url, re.IGNORECASE):
        # Убираем всё между схемой и tiktok.com, добавляем kk
        return re.sub(r"(?i)https?://[^/]*tiktok\.com", "https://kktiktok.com", url)
    if re.search(r"instagram\.com", url, re.IGNORECASE):
        return re.sub(r"(?i)https?://(?:www\.)?instagram\.com", "https://kkinstagram.com", url)
    # Fallback для других платформ
    url = re.sub(r"(?i)https?://www\.", "https://kk", url)
    url = re.sub(r"(?i)(https?://)(?!kk)", r"\1kk", url)
    return url


# ─── FFmpeg-обработка ─────────────────────────────────────────────────────────
def get_pixel_format(file_path: str) -> str | None:
    """Определяет pixel format видеопотока через ffprobe."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=pix_fmt",
             "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            capture_output=True, timeout=10,
        )
        return r.stdout.decode().strip() or None
    except Exception:
        return None


def process_video(input_path: str, output_path: str, force_reencode: bool = False) -> bool:
    """
    Обрабатывает видео для Telegram.

    Логика:
    • force_reencode=True (YouTube): всегда перекодируем в libx264/yuv420p.
      Это исправляет белый экран и стопкадр у Shorts/VP9/10-bit роликов.
      Оптимизация: если файл уже avc+yuv420p — только faststart (быстро).

    • force_reencode=False (остальные): только убираем метаданные и
      переставляем moov в начало. Потоки копируются — очень быстро.
    """
    try:
        if force_reencode:
            pix_fmt = get_pixel_format(input_path)
            # Если уже совместимый формат — просто faststart без перекодировки
            if pix_fmt == "yuv420p":
                cmd = [
                    "ffmpeg", "-y", "-i", input_path,
                    "-map_metadata", "-1",
                    "-map", "0:v?", "-map", "0:a?",
                    "-c", "copy",
                    "-movflags", "+faststart",
                    output_path,
                ]
                timeout = 60
            else:
                # Полная перекодировка: ultrafast — максимальная скорость,
                # небольшая потеря качества по сравнению с fast/medium,
                # но для Telegram-просмотра разница незаметна.
                cmd = [
                    "ffmpeg", "-y", "-i", input_path,
                    "-map_metadata", "-1",
                    "-map", "0:v?", "-map", "0:a?",
                    "-c:v", "libx264",
                    "-preset", "ultrafast",   # быстрее fast в ~3–4 раза
                    "-crf", "23",
                    "-pix_fmt", "yuv420p",
                    "-profile:v", "high",
                    "-level:v", "4.1",
                    "-threads", "0",          # задействовать все ядра
                    "-c:a", "copy",
                    "-movflags", "+faststart",
                    output_path,
                ]
                timeout = 300
        else:
            # Только метаданные + faststart, без перекодировки
            cmd = [
                "ffmpeg", "-y", "-i", input_path,
                "-map_metadata", "-1",
                "-map", "0:v?", "-map", "0:a?",
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
        logger.error(f"process_video error: {e}")
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


def fmt_speed(bps) -> str:
    if not bps:
        return ""
    mbps = bps / 1_048_576
    return f"{mbps:.1f} МБ/с" if mbps >= 1 else f"{bps/1024:.0f} КБ/с"


def fmt_eta(seconds) -> str:
    if not seconds:
        return ""
    if seconds < 60:
        return f"~{int(seconds)} сек"
    return f"~{int(seconds // 60)} мин {int(seconds % 60)} сек"


# ─── Скачивание ───────────────────────────────────────────────────────────────
class DownloadCancelled(Exception):
    pass


def download_video(
    url: str,
    output_dir: str,
    cancel_event: threading.Event | None = None,
    status_callback=None,       # callable(text: str) — для обновления статуса
) -> dict | None:
    """
    Скачивает видео. Сообщает о прогрессе через status_callback.
    Поддерживает отмену через cancel_event.
    """
    yt = is_youtube(url)
    video_format = YT_FORMAT if yt else DEFAULT_FORMAT

    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
    last_status_time = [0.0]   # изменяемый контейнер для nonlocal-доступа

    def progress_hook(d):
        if cancel_event and cancel_event.is_set():
            raise DownloadCancelled()

        if not status_callback:
            return

        now = time.monotonic()
        if now - last_status_time[0] < 2.5:   # не чаще раза в 2.5 сек
            return
        last_status_time[0] = now

        status = d.get("status", "")
        if status == "downloading":
            downloaded = d.get("downloaded_bytes") or 0
            total      = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            speed      = d.get("speed")
            eta        = d.get("eta")

            lines = ["⏳ Скачиваю видео..."]
            if total and downloaded:
                pct = int(downloaded / total * 100)
                done_mb  = downloaded / 1_048_576
                total_mb = total / 1_048_576
                lines.append(f"📊 {pct}%  ({done_mb:.1f} / {total_mb:.1f} МБ)")
            speed_str = fmt_speed(speed)
            eta_str   = fmt_eta(eta)
            if speed_str or eta_str:
                lines.append(f"🚀 {speed_str}  {eta_str}".strip())

            status_callback("\n".join(lines))

        elif status == "finished":
            status_callback("⚙️ Обрабатываю видео...")

    ydl_opts = {
        "format":             video_format,
        "outtmpl":            output_template,
        "quiet":              True,
        "no_warnings":        True,
        "noplaylist":         True,
        "max_filesize":       MAX_FILE_SIZE_BYTES,
        "merge_output_format":"mp4",
        "postprocessors":     [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
        "progress_hooks":     [progress_hook],
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

            if cancel_event and cancel_event.is_set():
                raise DownloadCancelled()

            # Обработка (ffmpeg) — только YouTube перекодирует при необходимости
            clean_path = os.path.join(output_dir, "clean.mp4")
            if process_video(filename, clean_path, force_reencode=yt) and Path(clean_path).exists():
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
    sender_name: str = "",
    sender_username: str = "",
) -> str:
    parts = []

    if show_stats and stats_str:
        parts.append(stats_str)

    if show_desc and description:
        sep = "\n\n" if parts else ""
        parts.append(f"{sep}📝 {description}")

    link_line = f"🔗 <a href='{url}'>Оригинал</a>  •  🤖 <a href='{BOT_LINK}'>@{BOT_USERNAME}</a>"
    parts.append(f"\n\n{link_line}" if parts else link_line)

    # Кто поделился — курсив, мелко, после основного блока
    if sender_name:
        if sender_username:
            sender_line = (
                f"\n\n<i><a href='https://t.me/{sender_username}'>"
                f"{sender_name}"
                f"</a></i>"
            )
        else:
            sender_line = f"\n\n<i>{sender_name}</i>"
        parts.append(sender_line)

    return "".join(parts)


# ─── Клавиатуры ───────────────────────────────────────────────────────────────
def make_cancel_keyboard(chat_id: int, status_msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🚫 Отмена", callback_data=f"cancel:{chat_id}:{status_msg_id}"),
    ]])


def make_main_keyboard(
    chat_id: int, msg_id: int,
    is_kk: bool, kk_active: bool = False,
    sender_user_id: int = 0,
) -> InlineKeyboardMarkup:
    del_btn = InlineKeyboardButton(
        "🗑️", callback_data=f"del:{chat_id}:{msg_id}:{sender_user_id}"
    )
    if is_kk:
        bot_label = "🤖 Через Бот ✅" if not kk_active else "🤖 Через Бот"
        kk_label  = "🔗 Через kk ✅"  if kk_active     else "🔗 Через kk"
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(bot_label, callback_data=f"sw_bot:{chat_id}:{msg_id}"),
                InlineKeyboardButton(kk_label,  callback_data=f"sw_kk:{chat_id}:{msg_id}"),
                del_btn,
            ],
            [InlineKeyboardButton("ℹ️ Доп.инфа", callback_data=f"info:{chat_id}:{msg_id}")],
        ])
    else:
        return make_toggle_keyboard(
            chat_id, msg_id, show_desc=True, show_stats=True,
            back=False, sender_user_id=sender_user_id,
        )


def make_toggle_keyboard(
    chat_id: int, msg_id: int,
    show_desc: bool, show_stats: bool,
    back: bool = False,
    sender_user_id: int = 0,
) -> InlineKeyboardMarkup:
    d = "✅" if show_desc  else "☑️"
    s = "✅" if show_stats else "☑️"
    del_btn = InlineKeyboardButton(
        "🗑️", callback_data=f"del:{chat_id}:{msg_id}:{sender_user_id}"
    )
    rows = [
        [
            InlineKeyboardButton(f"{d} Описание",   callback_data=f"tog:{chat_id}:{msg_id}:desc"),
            InlineKeyboardButton(f"{s} Статистика", callback_data=f"tog:{chat_id}:{msg_id}:stats"),
            del_btn,
        ],
        [
            InlineKeyboardButton("✅ Всё",           callback_data=f"tog:{chat_id}:{msg_id}:all"),
            InlineKeyboardButton("❌ Только ссылка", callback_data=f"tog:{chat_id}:{msg_id}:none"),
        ],
    ]
    if back:
        rows.append([InlineKeyboardButton("← Назад", callback_data=f"back:{chat_id}:{msg_id}")])
    return InlineKeyboardMarkup(rows)


# ─── Отправка видео ────────────────────────────────────────────────────────────
async def process_and_send_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    reply_to: int | None = None,
    sender_name: str = "",
    sender_username: str = "",
    sender_user_id: int = 0,
) -> None:
    chat_id = update.effective_chat.id
    prefs   = context.user_data.get("prefs", dict(DEFAULT_PREFS))
    kk      = is_kk_platform(url)

    # Статусное сообщение с кнопкой Отмена
    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="⏳ Скачиваю видео, подожди немного...",
        reply_to_message_id=reply_to,
        reply_markup=make_cancel_keyboard(chat_id, 0),
    )
    await context.bot.edit_message_reply_markup(
        chat_id=chat_id, message_id=status_msg.message_id,
        reply_markup=make_cancel_keyboard(chat_id, status_msg.message_id),
    )
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

    cancel_event = threading.Event()
    cancel_key   = f"cancel:{chat_id}:{status_msg.message_id}"
    context.bot_data[cancel_key] = cancel_event

    # Callback для обновления статуса из потока yt-dlp
    loop = asyncio.get_event_loop()
    last_text  = [""]

    def status_callback(text: str) -> None:
        if cancel_event.is_set():
            return
        if text == last_text[0]:
            return
        last_text[0] = text
        full_text = text + "\n\n🚫 Нажми Отмена, чтобы остановить"
        asyncio.run_coroutine_threadsafe(
            context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg.message_id,
                text=full_text,
                reply_markup=make_cancel_keyboard(chat_id, status_msg.message_id),
            ),
            loop,
        )

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            r = await loop.run_in_executor(
                None, download_video, url, tmpdir, cancel_event, status_callback
            )

            if cancel_event.is_set():
                return   # on_callback уже удалил status_msg

            # ── Fallback kk ─────────────────────────────────────────────────
            if r is None and kk:
                kk_url = to_kk_url(url)
                await status_msg.edit_text(
                    "⚠️ Не удалось скачать напрямую. Пробую через kk...",
                    reply_markup=make_cancel_keyboard(chat_id, status_msg.message_id),
                )
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"🔗 <a href='{kk_url}'>{kk_url}</a>\n\n🤖 <a href='{BOT_LINK}'>@{BOT_USERNAME}</a>",
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
                    parse_mode=ParseMode.HTML, reply_markup=None,
                )
                return

            stats_str = build_stats_str(r)
            caption   = build_caption(
                url=url, description=r["description"], stats_str=stats_str,
                show_desc=prefs["desc"], show_stats=prefs["stats"],
                sender_name=sender_name,
                sender_username=sender_username,
            )

            try:
                await status_msg.edit_text(
                    "📤 Отправляю видео...",
                    reply_markup=make_cancel_keyboard(chat_id, status_msg.message_id),
                )
                with open(r["path"], "rb") as vf:
                    sent = await context.bot.send_video(
                        chat_id=chat_id, video=vf,
                        caption=caption, parse_mode=ParseMode.HTML,
                        duration=r.get("duration"),
                        width=r.get("width"), height=r.get("height"),
                        supports_streaming=True,
                        reply_to_message_id=reply_to,
                    )

                kb = make_main_keyboard(chat_id, sent.message_id, is_kk=kk, kk_active=False, sender_user_id=sender_user_id)
                await context.bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=sent.message_id, reply_markup=kb
                )

                # Сохраняем file_id для переключения без повторной загрузки
                file_id = sent.video.file_id if sent.video else None

                context.bot_data[f"vid:{chat_id}:{sent.message_id}"] = {
                    "url":          url,
                    "kk_url":       to_kk_url(url) if kk else "",
                    "is_kk":        kk,
                    "kk_active":    False,
                    "file_id":      file_id,     # для повторной отправки без загрузки
                    "kk_msg_id":    None,         # id текущего kk-сообщения (если активно)
                    "bot_msg_id":   sent.message_id,
                    "title":        r["title"],
                    "description":  r["description"],
                    "stats_str":    stats_str,
                    "show_desc":    prefs["desc"],
                    "show_stats":   prefs["stats"],
                    "sender_name":  sender_name,
                    "sender_username": sender_username,
                    "sender_user_id": sender_user_id,
                    "duration":     r.get("duration"),
                    "width":        r.get("width"),
                    "height":       r.get("height"),
                    "reply_to":     reply_to,
                }
                await status_msg.delete()

            except TelegramError as e:
                logger.error(f"Ошибка отправки: {e}")
                await status_msg.edit_text(
                    "❌ Не удалось отправить (файл слишком большой).\n\n"
                    f"🔗 <a href='{url}'>Смотри по ссылке</a>",
                    parse_mode=ParseMode.HTML, reply_markup=None,
                )
    finally:
        context.bot_data.pop(cancel_key, None)


# ─── Callbacks ────────────────────────────────────────────────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query  = update.callback_query
    await query.answer()
    parts  = query.data.split(":")
    action = parts[0]

    # ── Отмена ───────────────────────────────────────────────────────────────
    if action == "cancel":
        if len(parts) < 3:
            return
        c_chat_id   = int(parts[1])
        c_status_id = int(parts[2])
        ev: threading.Event | None = context.bot_data.pop(
            f"cancel:{c_chat_id}:{c_status_id}", None
        )
        if ev:
            ev.set()
        try:
            await context.bot.delete_message(chat_id=c_chat_id, message_id=c_status_id)
        except TelegramError:
            pass
        await query.answer("🚫 Отменено", show_alert=False)
        return

    # ── Удаление сообщения ────────────────────────────────────────────────────
    if action == "del":
        # callback_data = "del:{chat_id}:{msg_id}:{sender_user_id}"
        if len(parts) < 4:
            return
        d_chat_id       = int(parts[1])
        d_msg_id        = int(parts[2])
        allowed_user_id = int(parts[3])
        presser_id      = query.from_user.id

        # Разрешаем удаление: автор ссылки или администратор чата
        can_delete = (presser_id == allowed_user_id)
        if not can_delete:
            # Проверяем, является ли пользователь администратором
            try:
                member = await context.bot.get_chat_member(d_chat_id, presser_id)
                if member.status in ("administrator", "creator"):
                    can_delete = True
            except TelegramError:
                pass

        if not can_delete:
            await query.answer(
                "🚫 Только тот, кто поделился ссылкой, может удалить это сообщение.",
                show_alert=True,
            )
            return

        # Удаляем само видео-сообщение
        try:
            await context.bot.delete_message(chat_id=d_chat_id, message_id=d_msg_id)
        except TelegramError:
            pass

        # Также удаляем kk-сообщение, если оно было
        key  = f"vid:{d_chat_id}:{d_msg_id}"
        data = context.bot_data.pop(key, None)
        if data and data.get("kk_msg_id"):
            try:
                await context.bot.delete_message(
                    chat_id=d_chat_id, message_id=data["kk_msg_id"]
                )
            except TelegramError:
                pass

        return

    # ── /settings ─────────────────────────────────────────────────────────────
    if action == "pref":
        sub   = parts[1]
        prefs = context.user_data.get("prefs", dict(DEFAULT_PREFS))
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

    # ── Все кнопки под видео требуют chat_id + msg_id ─────────────────────────
    if len(parts) < 3:
        return
    chat_id = int(parts[1])
    msg_id  = int(parts[2])
    key     = f"vid:{chat_id}:{msg_id}"
    data    = context.bot_data.get(key)

    if not data:
        await query.answer("Данные устарели. Отправь ссылку заново.", show_alert=True)
        return

    # ── Переключение: Через Бот ───────────────────────────────────────────────
    if action == "sw_bot":
        if not data.get("kk_active"):
            await query.answer("Видео уже через бота ✅", show_alert=False)
            return

        # Удаляем kk-сообщение
        if data.get("kk_msg_id"):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=data["kk_msg_id"])
            except TelegramError:
                pass
            data["kk_msg_id"] = None

        # Восстанавливаем видео через file_id (без повторного скачивания)
        caption = build_caption(
            url=data["url"], description=data["description"],
            stats_str=data["stats_str"],
            show_desc=data["show_desc"], show_stats=data["show_stats"],
            sender_name=data.get("sender_name", ""),
            sender_username=data.get("sender_username", ""),
        )
        try:
            sent = await context.bot.send_video(
                chat_id=chat_id,
                video=data["file_id"],
                caption=caption, parse_mode=ParseMode.HTML,
                duration=data.get("duration"),
                width=data.get("width"), height=data.get("height"),
                supports_streaming=True,
                reply_to_message_id=data.get("reply_to"),
            )
            # Удаляем старое сообщение бота (если существует, кроме текущего)
            if data.get("bot_msg_id") and data["bot_msg_id"] != sent.message_id:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=data["bot_msg_id"])
                except TelegramError:
                    pass

            new_msg_id = sent.message_id
            kb = make_main_keyboard(chat_id, new_msg_id, is_kk=True, kk_active=False, sender_user_id=data.get("sender_user_id", 0))
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=new_msg_id, reply_markup=kb
            )
            # Переносим запись в bot_data под новым ключом
            context.bot_data.pop(key, None)
            data["kk_active"]  = False
            data["bot_msg_id"] = new_msg_id
            context.bot_data[f"vid:{chat_id}:{new_msg_id}"] = data

        except TelegramError as e:
            logger.error(f"sw_bot error: {e}")
        return

    # ── Переключение: Через kk ────────────────────────────────────────────────
    if action == "sw_kk":
        if data.get("kk_active"):
            await query.answer("Уже через kk ✅", show_alert=False)
            return

        kk_url = data.get("kk_url") or to_kk_url(data["url"])

        # Удаляем текущее видео-сообщение бота
        if data.get("bot_msg_id"):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=data["bot_msg_id"])
            except TelegramError:
                pass
            data["bot_msg_id"] = None

        # Отправляем kk-ссылку с превью
        caption_kk = (
            f"🔗 <a href='{kk_url}'>{kk_url}</a>\n\n"
            f"🤖 <a href='{BOT_LINK}'>@{BOT_USERNAME}</a>"
        )
        try:
            sent_kk = await context.bot.send_message(
                chat_id=chat_id,
                text=caption_kk,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=data.get("reply_to"),
                disable_web_page_preview=False,
            )
            new_kk_msg_id = sent_kk.message_id

            # Добавляем кнопки под kk-сообщение
            kb = make_main_keyboard(chat_id, msg_id, is_kk=True, kk_active=True, sender_user_id=data.get("sender_user_id", 0))
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=new_kk_msg_id, reply_markup=kb
            )
            data["kk_active"] = True
            data["kk_msg_id"] = new_kk_msg_id
            # Оставляем ключ как есть (msg_id не меняется — это исходный id видео)
            context.bot_data[key] = data

        except TelegramError as e:
            logger.error(f"sw_kk error: {e}")
        return

    # ── Доп.инфа ─────────────────────────────────────────────────────────────
    if action == "info":
        sd, ss = data["show_desc"], data["show_stats"]
        await query.edit_message_reply_markup(
            reply_markup=make_toggle_keyboard(chat_id, msg_id, sd, ss, back=True, sender_user_id=data.get("sender_user_id", 0))
        )
        return

    # ── Назад ─────────────────────────────────────────────────────────────────
    if action == "back":
        await query.edit_message_reply_markup(
            reply_markup=make_main_keyboard(chat_id, msg_id,
                                            is_kk=data["is_kk"],
                                            kk_active=data.get("kk_active", False),
                                            sender_user_id=data.get("sender_user_id", 0))
        )
        return

    # ── Переключение описания / статистики ────────────────────────────────────
    if action == "tog":
        sub    = parts[3] if len(parts) > 3 else ""
        sd, ss = data["show_desc"], data["show_stats"]
        if   sub == "desc":  sd = not sd
        elif sub == "stats": ss = not ss
        elif sub == "all":   sd, ss = True, True
        elif sub == "none":  sd, ss = False, False

        data["show_desc"], data["show_stats"] = sd, ss
        context.bot_data[key] = data

        new_cap = build_caption(
            url=data["url"], description=data["description"],
            stats_str=data["stats_str"], show_desc=sd, show_stats=ss,
            sender_name=data.get("sender_name", ""),
            sender_username=data.get("sender_username", ""),
        )
        kb = make_toggle_keyboard(chat_id, msg_id, sd, ss, back=data["is_kk"], sender_user_id=data.get("sender_user_id", 0))
        await query.edit_message_caption(
            caption=new_cap, parse_mode=ParseMode.HTML, reply_markup=kb
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
        "<b>Кнопки под Instagram / TikTok:</b>\n"
        "🤖 Через Бот / 🔗 Через kk — переключают вариант видео в чате\n"
        "ℹ️ Доп.инфа — описание и статистика\n\n"
        "<b>Кнопки под YouTube и другими:</b>\n"
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
    sender_name = ""
    sender_username = ""
    if update.effective_chat.type in ("group", "supergroup"):
        user = msg.from_user
        if user:
            sender_name    = user.full_name or user.first_name or ""
            sender_username = user.username or ""
    # В личке тоже сохраняем user_id — чтобы владелец мог удалить
    sender_user_id = msg.from_user.id if msg.from_user else 0
    await process_and_send_video(
        update, context, url, reply_to=msg.message_id,
        sender_name=sender_name, sender_username=sender_username,
        sender_user_id=sender_user_id,
    )


# ─── Инлайн ───────────────────────────────────────────────────────────────────
async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query
    url   = extract_url(query.query.strip()) if query else None
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
    url = (context.bot_data.pop(f"inline_{result.inline_message_id}", None)
           or extract_url(result.query))
    if not url:
        return
    user_id = result.from_user.id
    prefs   = context.user_data.get("prefs", dict(DEFAULT_PREFS))
    kk      = is_kk_platform(url)
    try:
        await context.bot.send_message(chat_id=user_id, text=f"⏳ Скачиваю...\n🔗 {url}")
        with tempfile.TemporaryDirectory() as tmpdir:
            r = await asyncio.get_event_loop().run_in_executor(
                None, download_video, url, tmpdir
            )
            if r:
                stats_str = build_stats_str(r)
                caption   = build_caption(
                    url=url, description=r["description"], stats_str=stats_str,
                    show_desc=prefs["desc"], show_stats=prefs["stats"],
                )
                with open(r["path"], "rb") as vf:
                    sent = await context.bot.send_video(
                        chat_id=user_id, video=vf,
                        caption=caption, parse_mode=ParseMode.HTML,
                        duration=r.get("duration"),
                        width=r.get("width"), height=r.get("height"),
                        supports_streaming=True,
                    )
                file_id = sent.video.file_id if sent.video else None
                kb = make_main_keyboard(user_id, sent.message_id, is_kk=kk, kk_active=False)
                await context.bot.edit_message_reply_markup(
                    chat_id=user_id, message_id=sent.message_id, reply_markup=kb
                )
                context.bot_data[f"vid:{user_id}:{sent.message_id}"] = {
                    "url": url, "kk_url": to_kk_url(url) if kk else "",
                    "is_kk": kk, "kk_active": False, "file_id": file_id,
                    "kk_msg_id": None, "bot_msg_id": sent.message_id,
                    "title": r["title"], "description": r["description"],
                    "stats_str": stats_str,
                    "show_desc": prefs["desc"], "show_stats": prefs["stats"],
                    "sender_name": "", "duration": r.get("duration"),
                    "width": r.get("width"), "height": r.get("height"), "reply_to": None,
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
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(InlineQueryHandler(inline_query))
    logger.info("🎬 Бот, Смотри прикол — запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
