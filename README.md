# 🎬 Бот, смотри прикол! — Telegram бот для скачивания видео

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

## 🚀 Деплой — 3 способа

---

### Способ 1: Railway (рекомендуется, бесплатно до ~$5/мес)

**Railway** — самый простой вариант. Деплой в 5 кликов, 24/7.

#### Шаг 1: Создать бота в Telegram

1. Открой [@BotFather](https://t.me/BotFather) в Telegram
2. Отправь `/newbot`
3. Придумай имя и username (например `my_video_downloader_bot`)
4. Скопируй **токен** (выглядит как `1234567890:ABCDEFgh...`)
5. Отправь `/setinline` → выбери своего бота → напиши placeholder, например: `Вставь ссылку на видео...`
6. Отправь `/setinlinefeedback` → выбери бота → `Enable`

#### Шаг 2: Залить код на GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/ВАШ_USERNAME/videobot.git
git push -u origin main
```

#### Шаг 3: Создать проект на Railway

1. Зайди на [railway.app](https://railway.app) → Sign in with GitHub
2. Нажми **New Project** → **Deploy from GitHub repo**
3. Выбери свой репозиторий `videobot`
4. Railway автоматически обнаружит `nixpacks.toml` и установит ffmpeg

#### Шаг 4: Добавить переменные окружения

В Railway → вкладка **Variables** → добавь:

```
BOT_TOKEN = 1234567890:ABCDEFgh...
BOT_USERNAME = my_video_downloader_bot
```

#### Шаг 5: Деплой

Railway автоматически запустит бота. Статус смотри во вкладке **Deployments**.

**Автообновление:** при каждом `git push` Railway автоматически передеплоит.

---

### Способ 2: VPS / Сервер (Ubuntu)

Подходит если есть свой сервер или VPS (Hetzner, DigitalOcean, Timeweb и т.д.).

```bash
# 1. Обновить систему и установить зависимости
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.11 python3-pip ffmpeg git

# 2. Клонировать проект
git clone https://github.com/ВАШ_USERNAME/videobot.git
cd videobot

# 3. Установить Python-зависимости
pip3 install -r requirements.txt

# 4. Настроить переменные
cp .env.example .env
nano .env
# Вставь BOT_TOKEN и BOT_USERNAME, сохрани (Ctrl+X, Y, Enter)

# 5. Запустить с переменными окружения
export $(cat .env | xargs)
python3 bot.py
```

**Для работы 24/7 через systemd:**

```bash
sudo nano /etc/systemd/system/videobot.service
```

Вставь:
```ini
[Unit]
Description=VideoBot Telegram
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/videobot
EnvironmentFile=/home/ubuntu/videobot/.env
ExecStart=/usr/bin/python3 bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable videobot
sudo systemctl start videobot
sudo systemctl status videobot   # проверить статус
journalctl -u videobot -f        # смотреть логи
```

---

### Способ 3: Docker (любой хостинг с Docker)

```bash
# Скопируй .env.example в .env и заполни
cp .env.example .env
nano .env

# Запуск
docker-compose up -d

# Логи
docker-compose logs -f

# Остановка
docker-compose down
```

Работает на любом хостинге: VPS, Fly.io, Render и т.д.

---

## ⚙️ Переменные окружения

| Переменная | Описание | Пример |
|---|---|---|
| `BOT_TOKEN` | Токен от @BotFather | `1234567890:ABCDEFgh...` |
| `BOT_USERNAME` | Username бота без @ | `my_video_bot` |

---

## 📖 Как пользоваться ботом

### Личный чат
Просто отправь боту ссылку на видео:
```
https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

### В группе
1. Добавь бота в группу
2. Дай ему права на отправку сообщений
3. Любой участник может отправить ссылку — бот автоматически скачает видео

### Инлайн-режим
В любом чате Telegram напиши:
```
@your_bot_name https://www.tiktok.com/...
```
Выбери результат — видео будет отправлено в текущий чат.

---

## 🎯 Поддерживаемые платформы

- YouTube / YouTube Shorts
- TikTok
- Instagram Reels / Posts
- Twitter / X
- Vimeo
- Reddit (видео-посты)
- Twitch (клипы)
- Dailymotion
- Facebook Watch
- И 1000+ других сайтов через yt-dlp

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
