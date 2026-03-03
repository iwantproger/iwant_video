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
from datetime import date
from pathlib import Path

import yt_dlp
from telegram import (
    Update,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
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
BOT_TOKEN     = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
BOT_USERNAME  = os.environ.get("BOT_USERNAME", "your_bot_username")
BOT_LINK      = f"https://t.me/{BOT_USERNAME}"
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))

MAX_FILE_SIZE_MB    = 50
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

YT_FORMAT = (
    "bestvideo[vcodec^=avc][ext=mp4][filesize<45M]+bestaudio[ext=m4a]"
    "/bestvideo[vcodec^=avc][filesize<45M]+bestaudio"
    "/bestvideo[ext=mp4][filesize<45M]+bestaudio[ext=m4a]"
    "/best[ext=mp4][filesize<45M]/best[filesize<45M]/best"
)
DEFAULT_FORMAT = (
    "bestvideo[ext=mp4][filesize<45M]+bestaudio[ext=m4a]"
    "/best[ext=mp4][filesize<45M]/best[filesize<45M]/best"
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# По умолчанию — минимум: только ссылка
DEFAULT_PREFS = {
    "desc":        False,  # показывать описание видео
    "stats":       False,  # показывать статистику (просмотры, лайки)
    "auto_delete": True,   # удалять исходное сообщение если только ссылка
    "show_sender": True,   # показывать «Отправил:» в группах
}

# Состояния диалога
STATE_IDLE            = "idle"
STATE_SUPPORT         = "support"        # пользователь пишет в поддержку
STATE_BROADCAST_INPUT = "broadcast"      # админ вводит текст рассылки
STATE_REPLY_SUPPORT   = "reply_support"  # админ отвечает пользователю


# ─── Reply-клавиатура нижнего меню ────────────────────────────────────────────
def main_menu_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("⚙️ Настройки"),  KeyboardButton("❓ Помощь")],
        [KeyboardButton("🆘 Поддержка"),   KeyboardButton("🔄 Сбросить")],
    ]
    if ADMIN_USER_ID and user_id == ADMIN_USER_ID:
        rows.append([
            KeyboardButton("📊 Статистика"),
            KeyboardButton("📢 Рассылка"),
        ])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


# ─── Статистика ───────────────────────────────────────────────────────────────
def get_stats(bot_data: dict) -> dict:
    if "stats" not in bot_data:
        bot_data["stats"] = {
            "users":      set(),   # все уникальные user_id
            "daily":      {},      # {date_str: set(user_id)}
            "links_sent": 0,
            "success":    0,
            "failed":     0,
            "per_user":   {},      # {user_id: {"sent": int, "success": int}}
        }
    return bot_data["stats"]


def track_request(bot_data: dict, user_id: int) -> None:
    s = get_stats(bot_data)
    s["users"].add(user_id)
    today = str(date.today())
    s["daily"].setdefault(today, set()).add(user_id)
    s["links_sent"] += 1
    s["per_user"].setdefault(user_id, {"sent": 0, "success": 0})["sent"] += 1


def track_success(bot_data: dict, user_id: int) -> None:
    s = get_stats(bot_data)
    s["success"] += 1
    s["per_user"].setdefault(user_id, {"sent": 0, "success": 0})["success"] += 1


def track_failed(bot_data: dict) -> None:
    get_stats(bot_data)["failed"] += 1


# ─── URL-утилиты ──────────────────────────────────────────────────────────────
URL_PATTERN = re.compile(
    r"(https?://(?:www\.)?"
    r"(?:youtube\.com/watch\?[^\s]+|youtu\.be/[^\s]+"
    r"|youtube\.com/shorts/[^\s]+"
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


def is_url_only(text: str, url: str) -> bool:
    return text.strip() == url.strip()


def is_youtube(url: str) -> bool:
    return bool(re.search(r"(youtube\.com|youtu\.be)", url, re.IGNORECASE))


def is_kk_platform(url: str) -> bool:
    return bool(re.search(r"(instagram\.com|tiktok\.com)", url, re.IGNORECASE))


def to_kk_url(url: str) -> str:
    if re.search(r"tiktok\.com", url, re.IGNORECASE):
        return re.sub(r"(?i)https?://[^/]*tiktok\.com", "https://kktiktok.com", url)
    if re.search(r"instagram\.com", url, re.IGNORECASE):
        return re.sub(r"(?i)https?://(?:www\.)?instagram\.com", "https://kkinstagram.com", url)
    url = re.sub(r"(?i)https?://www\.", "https://kk", url)
    url = re.sub(r"(?i)(https?://)(?!kk)", r"\1kk", url)
    return url


# ─── FFmpeg ───────────────────────────────────────────────────────────────────
def get_pixel_format(file_path: str) -> str | None:
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
    try:
        if force_reencode:
            pix_fmt = get_pixel_format(input_path)
            if pix_fmt == "yuv420p":
                cmd = ["ffmpeg", "-y", "-i", input_path,
                       "-map_metadata", "-1", "-map", "0:v?", "-map", "0:a?",
                       "-c", "copy", "-movflags", "+faststart", output_path]
                timeout = 60
            else:
                cmd = ["ffmpeg", "-y", "-i", input_path,
                       "-map_metadata", "-1", "-map", "0:v?", "-map", "0:a?",
                       "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                       "-pix_fmt", "yuv420p", "-profile:v", "high", "-level:v", "4.1",
                       "-threads", "0", "-c:a", "copy", "-movflags", "+faststart", output_path]
                timeout = 300
        else:
            cmd = ["ffmpeg", "-y", "-i", input_path,
                   "-map_metadata", "-1", "-map", "0:v?", "-map", "0:a?",
                   "-c", "copy", "-movflags", "+faststart", output_path]
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
    status_callback=None,
) -> dict | None:
    yt = is_youtube(url)
    video_format = YT_FORMAT if yt else DEFAULT_FORMAT
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
    last_status_time = [0.0]

    def progress_hook(d):
        if cancel_event and cancel_event.is_set():
            raise DownloadCancelled()
        if not status_callback:
            return
        now = time.monotonic()
        if now - last_status_time[0] < 2.5:
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
        "format":              video_format,
        "outtmpl":             output_template,
        "quiet":               True,
        "no_warnings":         True,
        "noplaylist":          True,
        "max_filesize":        MAX_FILE_SIZE_BYTES,
        "merge_output_format": "mp4",
        "postprocessors":      [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
        "progress_hooks":      [progress_hook],
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
                "title":         (info.get("title") or "").strip(),
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


# ─── Подпись ──────────────────────────────────────────────────────────────────
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
    title: str,
    description: str,
    stats_str: str,
    show_desc: bool,
    show_stats: bool,
    sender_name: str = "",
    sender_username: str = "",
    show_sender: bool = True,
) -> str:
    parts = []

    parts.append(f"<b>Смотри прикол! {title}</b>" if title else "<b>Смотри прикол!</b>")

    if show_stats and stats_str:
        parts.append(f"\n{stats_str}")

    if show_desc and description:
        parts.append(f"\n\n📝 {description}")

    parts.append(
        f"\n\n🔗 <a href='{url}'>Оригинал</a>  •  🤖 <a href='{BOT_LINK}'>@{BOT_USERNAME}</a>"
    )

    if show_sender and sender_name:
        if sender_username:
            parts.append(
                f"\n\n<i>Отправил: <a href='https://t.me/{sender_username}'>{sender_name}</a></i>"
            )
        else:
            parts.append(f"\n\n<i>Отправил: {sender_name}</i>")

    return "".join(parts)


# ─── Inline-клавиатуры под видео ──────────────────────────────────────────────
def make_cancel_keyboard(chat_id: int, status_msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🚫 Отмена", callback_data=f"cancel:{chat_id}:{status_msg_id}"),
    ]])


def make_single_settings_keyboard(chat_id: int, msg_id: int) -> InlineKeyboardMarkup:
    """Одна кнопка ⚙️ под видео."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⚙️", callback_data=f"open:{chat_id}:{msg_id}"),
    ]])


def make_expanded_keyboard(
    chat_id: int, msg_id: int,
    is_kk: bool, kk_active: bool = False,
    sender_user_id: int = 0,
) -> InlineKeyboardMarkup:
    del_btn      = InlineKeyboardButton("🗑️ Удалить",  callback_data=f"del:{chat_id}:{msg_id}:{sender_user_id}")
    info_btn     = InlineKeyboardButton("ℹ️ Доп.инфа", callback_data=f"info:{chat_id}:{msg_id}")
    collapse_btn = InlineKeyboardButton("✖️ Свернуть", callback_data=f"collapse:{chat_id}:{msg_id}")

    if is_kk:
        bot_label = "🤖 Через Бот ✅" if not kk_active else "🤖 Через Бот"
        kk_label  = "🔗 Через kk ✅"  if kk_active     else "🔗 Через kk"
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(bot_label, callback_data=f"sw_bot:{chat_id}:{msg_id}"),
                InlineKeyboardButton(kk_label,  callback_data=f"sw_kk:{chat_id}:{msg_id}"),
            ],
            [info_btn, del_btn],
            [collapse_btn],
        ])
    else:
        return InlineKeyboardMarkup([
            [info_btn, del_btn],
            [collapse_btn],
        ])


def make_info_keyboard(
    chat_id: int, msg_id: int,
    show_desc: bool, show_stats: bool,
    sender_user_id: int = 0,
) -> InlineKeyboardMarkup:
    d = "✅ Описание"   if show_desc  else "☑️ Описание"
    s = "✅ Статистика" if show_stats else "☑️ Статистика"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(d, callback_data=f"tog:{chat_id}:{msg_id}:desc"),
            InlineKeyboardButton(s, callback_data=f"tog:{chat_id}:{msg_id}:stats"),
        ],
        [
            InlineKeyboardButton("🗑️ Удалить",  callback_data=f"del:{chat_id}:{msg_id}:{sender_user_id}"),
            InlineKeyboardButton("💾 Сохранить", callback_data=f"save:{chat_id}:{msg_id}"),
        ],
        [InlineKeyboardButton("← Назад", callback_data=f"back:{chat_id}:{msg_id}")],
    ])


# ─── Настройки пользователя ───────────────────────────────────────────────────
def settings_text(prefs: dict) -> str:
    d  = "✅" if prefs.get("desc")         else "☑️"
    s  = "✅" if prefs.get("stats")        else "☑️"
    ad = "✅" if prefs.get("auto_delete", True)  else "☑️"
    ss = "✅" if prefs.get("show_sender", True)  else "☑️"
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"{d} <b>Описание</b> — показывать текст описания под видео\n"
        f"{s} <b>Статистика</b> — просмотры, лайки, комментарии\n"
        f"{ad} <b>Авто-удаление ссылок</b> — убирать сообщение с ссылкой после скачивания\n"
        f"{ss} <b>Показывать «Отправил:»</b> — имя отправителя в группах\n\n"
        "<i>Изменения применяются к следующим видео.</i>"
    )


def make_settings_keyboard(prefs: dict) -> InlineKeyboardMarkup:
    d  = "✅" if prefs.get("desc")               else "☑️"
    s  = "✅" if prefs.get("stats")              else "☑️"
    ad = "✅" if prefs.get("auto_delete", True)  else "☑️"
    ss = "✅" if prefs.get("show_sender", True)  else "☑️"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{d} Описание",   callback_data="pref:desc"),
            InlineKeyboardButton(f"{s} Статистика", callback_data="pref:stats"),
        ],
        [InlineKeyboardButton(f"{ad} Авто-удаление ссылок",    callback_data="pref:auto_delete")],
        [InlineKeyboardButton(f"{ss} Показывать «Отправил:»",  callback_data="pref:show_sender")],
    ])


# ─── Отправка видео ────────────────────────────────────────────────────────────
async def process_and_send_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    reply_to: int | None = None,
    sender_name: str = "",
    sender_username: str = "",
    sender_user_id: int = 0,
    delete_source_msg_id: int | None = None,
) -> None:
    chat_id = update.effective_chat.id
    prefs   = context.user_data.get("prefs", dict(DEFAULT_PREFS))
    uid     = sender_user_id or (update.effective_user.id if update.effective_user else 0)

    track_request(context.bot_data, uid)

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="⏳ Скачиваю видео, подожди немного...",
        reply_to_message_id=reply_to,
        reply_markup=make_cancel_keyboard(chat_id, 0),
        disable_notification=True,
    )
    await context.bot.edit_message_reply_markup(
        chat_id=chat_id, message_id=status_msg.message_id,
        reply_markup=make_cancel_keyboard(chat_id, status_msg.message_id),
    )
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

    cancel_event = threading.Event()
    cancel_key   = f"cancel:{chat_id}:{status_msg.message_id}"
    context.bot_data[cancel_key] = cancel_event

    loop      = asyncio.get_event_loop()
    last_text = [""]

    def status_callback(text: str) -> None:
        if cancel_event.is_set() or text == last_text[0]:
            return
        last_text[0] = text
        asyncio.run_coroutine_threadsafe(
            context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg.message_id,
                text=text + "\n\n🚫 Нажми Отмена, чтобы остановить",
                reply_markup=make_cancel_keyboard(chat_id, status_msg.message_id),
            ),
            loop,
        )

    kk = is_kk_platform(url)

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            r = await loop.run_in_executor(
                None, download_video, url, tmpdir, cancel_event, status_callback
            )

            if cancel_event.is_set():
                return

            if r is None:
                track_failed(context.bot_data)
                await status_msg.edit_text(
                    "❌ Не удалось скачать видео.\n\n"
                    "▪️ Видео недоступно или удалено\n"
                    "▪️ Файл больше 50 МБ\n"
                    "▪️ Сервис временно недоступен\n\n"
                    f"🔗 <a href='{url}'>Открыть по ссылке</a>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "🗑️ Удалить",
                            callback_data=f"del_status:{chat_id}:{status_msg.message_id}:{uid}"
                        )
                    ]]),
                )
                return

            stats_str = build_stats_str(r)
            caption   = build_caption(
                url=url, title=r["title"],
                description=r["description"], stats_str=stats_str,
                show_desc=prefs["desc"], show_stats=prefs["stats"],
                sender_name=sender_name, sender_username=sender_username,
                show_sender=prefs.get("show_sender", True),
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
                        disable_notification=True,
                    )

                file_id = sent.video.file_id if sent.video else None
                await context.bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=sent.message_id,
                    reply_markup=make_single_settings_keyboard(chat_id, sent.message_id),
                )

                context.bot_data[f"vid:{chat_id}:{sent.message_id}"] = {
                    "url":             url,
                    "kk_url":          to_kk_url(url) if kk else "",
                    "is_kk":           kk,
                    "kk_active":       False,
                    "file_id":         file_id,
                    "kk_msg_id":       None,
                    "bot_msg_id":      sent.message_id,
                    "title":           r["title"],
                    "description":     r["description"],
                    "stats_str":       stats_str,
                    "show_desc":       prefs["desc"],
                    "show_stats":      prefs["stats"],
                    "show_sender":     prefs.get("show_sender", True),
                    "sender_name":     sender_name,
                    "sender_username": sender_username,
                    "sender_user_id":  sender_user_id,
                    "duration":        r.get("duration"),
                    "width":           r.get("width"),
                    "height":          r.get("height"),
                    "reply_to":        reply_to,
                }
                await status_msg.delete()

                if delete_source_msg_id and prefs.get("auto_delete", True):
                    try:
                        await context.bot.delete_message(
                            chat_id=chat_id, message_id=delete_source_msg_id
                        )
                    except TelegramError as e:
                        logger.warning(f"Не удалось удалить исходное сообщение: {e}")

                track_success(context.bot_data, uid)

            except TelegramError as e:
                logger.error(f"Ошибка отправки: {e}")
                track_failed(context.bot_data)
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

    # ── Отмена скачивания ─────────────────────────────────────────────────────
    if action == "cancel":
        if len(parts) < 3:
            return
        c_chat_id, c_status_id = int(parts[1]), int(parts[2])
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

    # ── Удаление сообщения об ошибке ──────────────────────────────────────────
    if action == "del_status":
        if len(parts) < 4:
            return
        d_chat_id, d_msg_id, allowed_uid = int(parts[1]), int(parts[2]), int(parts[3])
        if query.from_user.id == allowed_uid or allowed_uid == 0:
            try:
                await context.bot.delete_message(chat_id=d_chat_id, message_id=d_msg_id)
            except TelegramError:
                pass
        else:
            await query.answer("🚫 Только автор может удалить.", show_alert=True)
        return

    # ── Удаление видео ────────────────────────────────────────────────────────
    if action == "del":
        if len(parts) < 4:
            return
        d_chat_id, d_msg_id, allowed_uid = int(parts[1]), int(parts[2]), int(parts[3])
        presser_id = query.from_user.id
        can_delete = (presser_id == allowed_uid)
        if not can_delete:
            try:
                member = await context.bot.get_chat_member(d_chat_id, presser_id)
                if member.status in ("administrator", "creator"):
                    can_delete = True
            except TelegramError:
                pass
        if not can_delete:
            await query.answer("🚫 Только тот, кто поделился ссылкой, может удалить.", show_alert=True)
            return
        try:
            await context.bot.delete_message(chat_id=d_chat_id, message_id=d_msg_id)
        except TelegramError:
            pass
        key  = f"vid:{d_chat_id}:{d_msg_id}"
        data = context.bot_data.pop(key, None)
        if data and data.get("kk_msg_id"):
            try:
                await context.bot.delete_message(chat_id=d_chat_id, message_id=data["kk_msg_id"])
            except TelegramError:
                pass
        return

    # ── Настройки (pref:*) ────────────────────────────────────────────────────
    if action == "pref":
        sub   = parts[1]
        prefs = context.user_data.get("prefs", dict(DEFAULT_PREFS))
        if sub in ("desc", "stats", "auto_delete", "show_sender"):
            prefs[sub] = not prefs.get(sub, DEFAULT_PREFS.get(sub, False))
        context.user_data["prefs"] = prefs
        await query.edit_message_text(
            settings_text(prefs),
            parse_mode=ParseMode.HTML,
            reply_markup=make_settings_keyboard(prefs),
        )
        return

    # ── Ответить пользователю (admin) ─────────────────────────────────────────
    if action == "reply_user":
        if len(parts) < 3:
            return
        target_uid  = int(parts[1])
        target_name = parts[2]
        context.user_data["state"]         = STATE_REPLY_SUPPORT
        context.user_data["reply_to_user"] = target_uid
        context.user_data["reply_to_name"] = target_name
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text=f"✏️ Напиши ответ для <b>{target_name}</b>:\n\n<i>(отправь /cancel для отмены)</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Остальные кнопки — для видео-сообщений (chat_id + msg_id)
    if len(parts) < 3:
        return
    chat_id = int(parts[1])
    msg_id  = int(parts[2])
    key     = f"vid:{chat_id}:{msg_id}"
    data    = context.bot_data.get(key)

    if not data:
        await query.answer("Данные устарели. Отправь ссылку заново.", show_alert=True)
        return

    # ── Открыть меню ──────────────────────────────────────────────────────────
    if action == "open":
        await query.edit_message_reply_markup(
            reply_markup=make_expanded_keyboard(
                chat_id, msg_id,
                is_kk=data["is_kk"], kk_active=data.get("kk_active", False),
                sender_user_id=data.get("sender_user_id", 0),
            )
        )
        return

    # ── Свернуть ──────────────────────────────────────────────────────────────
    if action == "collapse":
        await query.edit_message_reply_markup(
            reply_markup=make_single_settings_keyboard(chat_id, msg_id)
        )
        return

    # ── Доп.инфа ──────────────────────────────────────────────────────────────
    if action == "info":
        await query.edit_message_reply_markup(
            reply_markup=make_info_keyboard(
                chat_id, msg_id,
                data["show_desc"], data["show_stats"],
                sender_user_id=data.get("sender_user_id", 0),
            )
        )
        return

    # ── Назад ─────────────────────────────────────────────────────────────────
    if action == "back":
        await query.edit_message_reply_markup(
            reply_markup=make_expanded_keyboard(
                chat_id, msg_id,
                is_kk=data["is_kk"], kk_active=data.get("kk_active", False),
                sender_user_id=data.get("sender_user_id", 0),
            )
        )
        return

    # ── Сохранить настройки ───────────────────────────────────────────────────
    if action == "save":
        prefs = context.user_data.get("prefs", dict(DEFAULT_PREFS))
        prefs["desc"]  = data["show_desc"]
        prefs["stats"] = data["show_stats"]
        context.user_data["prefs"] = prefs
        await query.answer("💾 Настройки сохранены!", show_alert=False)
        await query.edit_message_reply_markup(
            reply_markup=make_single_settings_keyboard(chat_id, msg_id)
        )
        return

    # ── Через Бот ─────────────────────────────────────────────────────────────
    if action == "sw_bot":
        if not data.get("kk_active"):
            await query.answer("Видео уже через бота ✅", show_alert=False)
            return
        if data.get("kk_msg_id"):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=data["kk_msg_id"])
            except TelegramError:
                pass
            data["kk_msg_id"] = None

        caption = build_caption(
            url=data["url"], title=data.get("title", ""),
            description=data["description"], stats_str=data["stats_str"],
            show_desc=data["show_desc"], show_stats=data["show_stats"],
            sender_name=data.get("sender_name", ""),
            sender_username=data.get("sender_username", ""),
            show_sender=data.get("show_sender", True),
        )
        try:
            sent = await context.bot.send_video(
                chat_id=chat_id, video=data["file_id"],
                caption=caption, parse_mode=ParseMode.HTML,
                duration=data.get("duration"),
                width=data.get("width"), height=data.get("height"),
                supports_streaming=True,
                reply_to_message_id=data.get("reply_to"),
                disable_notification=True,
            )
            if data.get("bot_msg_id") and data["bot_msg_id"] != sent.message_id:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=data["bot_msg_id"])
                except TelegramError:
                    pass
            new_msg_id = sent.message_id
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=new_msg_id,
                reply_markup=make_single_settings_keyboard(chat_id, new_msg_id),
            )
            context.bot_data.pop(key, None)
            data["kk_active"]  = False
            data["bot_msg_id"] = new_msg_id
            context.bot_data[f"vid:{chat_id}:{new_msg_id}"] = data
        except TelegramError as e:
            logger.error(f"sw_bot error: {e}")
        return

    # ── Через kk ──────────────────────────────────────────────────────────────
    if action == "sw_kk":
        if data.get("kk_active"):
            await query.answer("Уже через kk ✅", show_alert=False)
            return
        kk_url = data.get("kk_url") or to_kk_url(data["url"])
        if data.get("bot_msg_id"):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=data["bot_msg_id"])
            except TelegramError:
                pass
            data["bot_msg_id"] = None
        try:
            sent_kk = await context.bot.send_message(
                chat_id=chat_id,
                text=f"🔗 <a href='{kk_url}'>{kk_url}</a>\n\n🤖 <a href='{BOT_LINK}'>@{BOT_USERNAME}</a>",
                parse_mode=ParseMode.HTML,
                reply_to_message_id=data.get("reply_to"),
                disable_web_page_preview=False,
                disable_notification=True,
            )
            new_kk_msg_id = sent_kk.message_id
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=new_kk_msg_id,
                reply_markup=make_single_settings_keyboard(chat_id, msg_id),
            )
            data["kk_active"] = True
            data["kk_msg_id"] = new_kk_msg_id
            context.bot_data[key] = data
        except TelegramError as e:
            logger.error(f"sw_kk error: {e}")
        return

    # ── Переключение описания / статистики ────────────────────────────────────
    if action == "tog":
        sub    = parts[3] if len(parts) > 3 else ""
        sd, ss = data["show_desc"], data["show_stats"]
        if   sub == "desc":  sd = not sd
        elif sub == "stats": ss = not ss

        data["show_desc"], data["show_stats"] = sd, ss
        context.bot_data[key] = data

        new_cap = build_caption(
            url=data["url"], title=data.get("title", ""),
            description=data["description"], stats_str=data["stats_str"],
            show_desc=sd, show_stats=ss,
            sender_name=data.get("sender_name", ""),
            sender_username=data.get("sender_username", ""),
            show_sender=data.get("show_sender", True),
        )
        await query.edit_message_caption(
            caption=new_cap, parse_mode=ParseMode.HTML,
            reply_markup=make_info_keyboard(
                chat_id, msg_id, sd, ss,
                sender_user_id=data.get("sender_user_id", 0),
            ),
        )
        return


# ─── Команды ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    get_stats(context.bot_data)["users"].add(user_id)
    await update.message.reply_text(
        "👋 <b>Привет! Я — Бот, Смотри прикол 🎬</b>\n\n"
        "Скидывай ссылки на видео — скачаю и пришлю прямо в чат.\n\n"
        "Поддерживаю:\n"
        "▪️ YouTube / Shorts\n"
        "▪️ TikTok\n"
        "▪️ Instagram Reels\n"
        "▪️ Twitter / X\n"
        "▪️ Vimeo, Reddit, Twitch и ещё 1000+ сайтов\n\n"
        "Используй кнопки меню снизу 👇",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(user_id),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prefs = context.user_data.get("prefs", dict(DEFAULT_PREFS))
    d  = "✅" if prefs.get("desc")              else "☑️"
    s  = "✅" if prefs.get("stats")             else "☑️"
    ad = "✅" if prefs.get("auto_delete", True) else "☑️"
    await update.message.reply_text(
        "📖 <b>Справка</b>\n\n"
        "<b>Как пользоваться:</b>\n"
        "1️⃣ Отправь ссылку на видео\n"
        "2️⃣ Бот скачает и пришлёт видео\n"
        f"3️⃣ <code>@{BOT_USERNAME} ссылка</code> — в любом чате\n\n"
        "<b>Кнопка ⚙️ под видео открывает:</b>\n"
        "🤖/🔗 — Через Бот или kk-зеркало (Инста/ТикТок)\n"
        "ℹ️ Доп.инфа — описание и статистика\n"
        "🗑️ Удалить — убрать сообщение\n"
        "💾 Сохранить — запомнить настройки\n\n"
        f"<b>Твои настройки:</b>\n"
        f"{d} Описание  {s} Статистика  {ad} Авто-удаление ссылок\n\n"
        "Лимит файла: 50 МБ",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(update.effective_user.id),
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prefs = context.user_data.get("prefs", dict(DEFAULT_PREFS))
    await update.message.reply_text(
        settings_text(prefs),
        parse_mode=ParseMode.HTML,
        reply_markup=make_settings_keyboard(prefs),
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сброс: очищает настройки и историю состояний."""
    context.user_data.clear()
    await update.message.reply_text(
        "🔄 <b>Готово!</b> Все настройки сброшены до стандартных.\n\n"
        "Можешь отправлять ссылки заново.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(update.effective_user.id),
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.get("state", STATE_IDLE)
    context.user_data["state"] = STATE_IDLE
    context.user_data.pop("reply_to_user", None)
    context.user_data.pop("reply_to_name", None)
    msg = "✖️ Отменено." if state != STATE_IDLE else "Нечего отменять."
    await update.message.reply_text(msg)


# ─── Статистика и рассылка (admin) ────────────────────────────────────────────
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if ADMIN_USER_ID and update.effective_user.id != ADMIN_USER_ID:
        return

    s               = get_stats(context.bot_data)
    today           = str(date.today())
    total           = len(s["users"])
    active          = len(s["daily"].get(today, set()))
    sent            = s["links_sent"]
    ok              = s["success"]
    fail            = s["failed"]
    total_processed = ok + fail
    ok_pct          = round(ok   / total_processed * 100) if total_processed else 0
    fail_pct        = round(fail / total_processed * 100) if total_processed else 0
    per_user        = s["per_user"]
    avg_req         = round(sum(v["sent"] for v in per_user.values()) / len(per_user), 1) if per_user else 0

    top5 = sorted(per_user.items(), key=lambda x: x[1]["sent"], reverse=True)[:5]
    top5_lines = "\n".join(
        f"  {i+1}. <code>{uid}</code> — {v['sent']} запр., {v['success']} успешно"
        for i, (uid, v) in enumerate(top5)
    ) or "  нет данных"

    await update.message.reply_text(
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"🟢 Активных сегодня: <b>{active}</b>\n\n"
        f"🔗 Ссылок отправлено: <b>{sent}</b>\n"
        f"✅ Успешно: <b>{ok}</b> ({ok_pct}%)\n"
        f"❌ Не удалось: <b>{fail}</b> ({fail_pct}%)\n\n"
        f"📈 Среднее запросов/пользователь: <b>{avg_req}</b>\n\n"
        f"🏆 Топ-5:\n{top5_lines}\n\n"
        f"<i>Статистика сбрасывается при перезапуске.</i>",
        parse_mode=ParseMode.HTML,
    )


async def do_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    s         = get_stats(context.bot_data)
    user_ids  = list(s["users"])
    sent_ok   = 0
    sent_fail = 0

    await update.message.reply_text(f"⏳ Отправляю {len(user_ids)} пользователям...")
    for uid in user_ids:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"📢 <b>Сообщение от бота</b>\n\n{text}",
                parse_mode=ParseMode.HTML,
            )
            sent_ok += 1
            await asyncio.sleep(0.05)
        except TelegramError:
            sent_fail += 1

    await update.message.reply_text(
        f"✅ Готово!\n📨 Доставлено: {sent_ok}\n❌ Не доставлено: {sent_fail}"
    )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if ADMIN_USER_ID and update.effective_user.id != ADMIN_USER_ID:
        return
    text = " ".join(context.args) if context.args else ""
    if text:
        await do_broadcast(update, context, text)
    else:
        context.user_data["state"] = STATE_BROADCAST_INPUT
        await update.message.reply_text(
            "📢 <b>Рассылка</b>\n\n"
            "Напиши текст — он уйдёт всем пользователям бота.\n\n"
            "<i>Отправь /cancel для отмены.</i>",
            parse_mode=ParseMode.HTML,
        )


# ─── Обработчик всех текстовых сообщений ─────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg  = update.message
    if not msg or not msg.text:
        return

    text    = msg.text
    user_id = msg.from_user.id if msg.from_user else 0
    state   = context.user_data.get("state", STATE_IDLE)

    # ── Кнопки нижнего меню пользователя ─────────────────────────────────────
    if text == "⚙️ Настройки":
        await cmd_settings(update, context)
        return

    if text == "❓ Помощь":
        await cmd_help(update, context)
        return

    if text == "🔄 Сбросить":
        await cmd_reset(update, context)
        return

    if text == "🆘 Поддержка":
        context.user_data["state"] = STATE_SUPPORT
        await msg.reply_text(
            "🆘 <b>Поддержка</b>\n\n"
            "Напиши своё сообщение — я передам его автору бота.\n"
            "Можно описать проблему, задать вопрос или предложить идею.\n\n"
            "<i>Отправь /cancel для отмены.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # ── Кнопки нижнего меню администратора ───────────────────────────────────
    is_admin = not ADMIN_USER_ID or user_id == ADMIN_USER_ID

    if text == "📊 Статистика" and is_admin:
        await cmd_stats(update, context)
        return

    if text == "📢 Рассылка" and is_admin:
        context.user_data["state"] = STATE_BROADCAST_INPUT
        await msg.reply_text(
            "📢 <b>Рассылка</b>\n\n"
            "Напиши текст — он уйдёт всем пользователям бота.\n\n"
            "<i>Отправь /cancel для отмены.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # ── Режим поддержки: пользователь пишет вопрос ───────────────────────────
    if state == STATE_SUPPORT:
        context.user_data["state"] = STATE_IDLE
        user   = msg.from_user
        name   = user.full_name or user.first_name or "Аноним"
        uname  = f"@{user.username}" if user.username else f"id: {user_id}"
        if ADMIN_USER_ID:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_USER_ID,
                    text=(
                        f"📩 <b>Сообщение в поддержку</b>\n\n"
                        f"👤 <b>{name}</b> ({uname})\n"
                        f"🆔 <code>{user_id}</code>\n\n"
                        f"💬 {text}"
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            f"✉️ Ответить {name}",
                            callback_data=f"reply_user:{user_id}:{name[:20]}"
                        )
                    ]]),
                )
            except TelegramError as e:
                logger.error(f"Ошибка отправки в поддержку: {e}")
        await msg.reply_text("✅ Сообщение отправлено! Постараюсь ответить как можно скорее.")
        return

    # ── Режим рассылки: админ вводит текст ───────────────────────────────────
    if state == STATE_BROADCAST_INPUT and is_admin:
        context.user_data["state"] = STATE_IDLE
        await do_broadcast(update, context, text)
        return

    # ── Режим ответа пользователю (admin) ────────────────────────────────────
    if state == STATE_REPLY_SUPPORT and is_admin:
        target_uid  = context.user_data.pop("reply_to_user", None)
        target_name = context.user_data.pop("reply_to_name", "пользователю")
        context.user_data["state"] = STATE_IDLE
        if target_uid:
            try:
                await context.bot.send_message(
                    chat_id=target_uid,
                    text=f"💬 <b>Ответ от поддержки:</b>\n\n{text}",
                    parse_mode=ParseMode.HTML,
                )
                await msg.reply_text(f"✅ Ответ отправлен {target_name}.")
            except TelegramError as e:
                await msg.reply_text(f"❌ Не удалось отправить: {e}")
        return

    # ── Обычная ссылка на видео ───────────────────────────────────────────────
    url = extract_url(text)
    if not url:
        if update.effective_chat.type == "private":
            await msg.reply_text(
                "🔍 Не нашёл ссылку.\n"
                "Отправь ссылку на YouTube, TikTok, Instagram и т.д.",
                reply_markup=main_menu_keyboard(user_id),
            )
        return

    sender_name     = ""
    sender_username = ""
    if update.effective_chat.type in ("group", "supergroup"):
        user = msg.from_user
        if user:
            sender_name     = user.full_name or user.first_name or ""
            sender_username = user.username or ""

    delete_source_msg_id = msg.message_id if is_url_only(text, url) else None

    await process_and_send_video(
        update, context, url,
        reply_to=msg.message_id,
        sender_name=sender_name,
        sender_username=sender_username,
        sender_user_id=user_id,
        delete_source_msg_id=delete_source_msg_id,
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
                f"🤖 <a href='{BOT_LINK}'>Бот, Смотри прикол</a>",
                parse_mode=ParseMode.HTML),
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
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🎬 Открыть бота", url=BOT_LINK)
        ]]),
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
    track_request(context.bot_data, user_id)
    try:
        await context.bot.send_message(
            chat_id=user_id, text=f"⏳ Скачиваю...\n🔗 {url}",
            disable_notification=True,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            r = await asyncio.get_event_loop().run_in_executor(
                None, download_video, url, tmpdir
            )
            if r:
                stats_str = build_stats_str(r)
                caption   = build_caption(
                    url=url, title=r["title"],
                    description=r["description"], stats_str=stats_str,
                    show_desc=prefs["desc"], show_stats=prefs["stats"],
                    show_sender=False,
                )
                with open(r["path"], "rb") as vf:
                    sent = await context.bot.send_video(
                        chat_id=user_id, video=vf,
                        caption=caption, parse_mode=ParseMode.HTML,
                        duration=r.get("duration"),
                        width=r.get("width"), height=r.get("height"),
                        supports_streaming=True, disable_notification=True,
                    )
                file_id = sent.video.file_id if sent.video else None
                await context.bot.edit_message_reply_markup(
                    chat_id=user_id, message_id=sent.message_id,
                    reply_markup=make_single_settings_keyboard(user_id, sent.message_id),
                )
                context.bot_data[f"vid:{user_id}:{sent.message_id}"] = {
                    "url": url, "kk_url": to_kk_url(url) if kk else "",
                    "is_kk": kk, "kk_active": False, "file_id": file_id,
                    "kk_msg_id": None, "bot_msg_id": sent.message_id,
                    "title": r["title"], "description": r["description"],
                    "stats_str": stats_str,
                    "show_desc": prefs["desc"], "show_stats": prefs["stats"],
                    "show_sender": False,
                    "sender_name": "", "sender_username": "",
                    "sender_user_id": user_id,
                    "duration": r.get("duration"),
                    "width": r.get("width"), "height": r.get("height"), "reply_to": None,
                }
                track_success(context.bot_data, user_id)
            else:
                track_failed(context.bot_data)
                await context.bot.send_message(
                    chat_id=user_id, text=f"❌ Не удалось скачать.\n🔗 {url}",
                    disable_notification=True,
                )
    except TelegramError as e:
        logger.error(f"chosen_inline_result error: {e}")


# ─── Запуск ───────────────────────────────────────────────────────────────────
def main() -> None:
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise ValueError("Установи BOT_TOKEN!")

    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("settings",  cmd_settings))
    app.add_handler(CommandHandler("reset",     cmd_reset))
    app.add_handler(CommandHandler("cancel",    cmd_cancel))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(InlineQueryHandler(inline_query))

    logger.info("🎬 Бот, Смотри прикол — запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
