# 🎬 VideoBot — Telegram бот для скачивания видео

Бот автоматически скачивает видео из YouTube, TikTok, Instagram, Twitter/X, Vimeo, Reddit и других сервисов, поддерживаемых [yt-dlp](https://github.com/yt-dlp/yt-dlp), и отправляет их прямо в Telegram.

---

## ✨ Возможности

| Функция | Описание |
|---|---|
| **Ссылки в чате** | Бот реагирует на любую ссылку на видео в группе или ЛС |
| **Подпись с источником** | Каждое видео содержит ссылку на оригинал и ссылку на бота |
| **Инлайн-режим** | `@bot https://...` — прямо из поля ввода любого чата |
| **Группы** | Можно добавить в любой групповой чат |
| **Многопоточность** | Несколько запросов обрабатываются параллельно |

---



## ⚠️ Ограничения

- **Максимальный размер файла: 50 МБ** (ограничение Telegram Bot API)
- Для видео > 50 МБ бот сообщит об ошибке и даст ссылку на оригинал
- Instagram может блокировать скачивание из-за политики приватности
- Для TikTok иногда требуется обновление yt-dlp: `pip install -U yt-dlp`

---

## 🔄 Обновление yt-dlp

yt-dlp обновляется часто (сайты меняют API). Для обновления:

```bash
pip install -U yt-dlp

# На Railway — просто сделай git push, Railway пересоберёт образ
```

---

## 🛠 Техническое устройство

```
bot.py                  — весь код бота
requirements.txt        — зависимости Python
Procfile               — команда запуска (Railway/Heroku)
railway.json           — конфиг Railway
nixpacks.toml          — установка ffmpeg на Railway
Dockerfile             — Docker-образ
docker-compose.yml     — запуск через Docker Compose
.env.example           — пример переменных окружения
.github/workflows/     — CI/CD через GitHub Actions
```

**Стек:**
- `python-telegram-bot 21` — библиотека для Telegram Bot API
- `yt-dlp` — скачивание видео (fork youtube-dl)
- `ffmpeg` — конвертация и мержинг видеопотоков
