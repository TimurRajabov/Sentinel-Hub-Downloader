# Sentinel Hub Downloader

A web application for automated batch downloading of **Sentinel-2** satellite imagery via Google Earth Engine (GEE).
Supports batch downloads by GeoJSON polygons, date ranges, multiple spectral products, and a Telegram bot assistant.

---

## Features

- Download Sentinel-2 L2A imagery by polygons from GeoJSON
- Product modes: **RGB**, **RGB+NIR**, **NDVI**, **Custom** (any bands)
- Single date or date range with configurable step (e.g. every 4 days)
- **Single raster** mode — one bbox for all polygons (1 request instead of N)
- Cloud masking + composite gap filling (fill-window strategy)
- Parallel tile downloading (ThreadPoolExecutor) with auto-retry and exponential backoff
- Multiple GEE account support (Service Account / User)
- Web UI with map, drag-and-drop upload, real-time job logs
- Job history and settings stored in SQLite
- **Telegram bot** with AI assistant (Ollama) for monitoring and notifications
- Docker / docker-compose support

---

## Quick Start — Local

### 1. Clone the repository

```bash
git clone <repo-url>
cd 10_10
```

### 2. Create virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install system dependencies (Debian/Ubuntu)

```bash
sudo apt-get install -y gdal-bin libgdal-dev libgeos-dev libproj-dev libspatialindex-dev build-essential
```

### 4. Install Python dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 5. Configure environment

Create a `.env` file in the project root:

```env
PROJECT_ID=your-gee-project-id
SAVE_PATH=/data

# Telegram bot
TELEGRAM_TOKEN=your-telegram-bot-token
OLLAMA_MODEL=qwen2.5:7b
```

### 6. Start the application

```bash
bash run.sh
# or
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Open in browser: **http://localhost:8080**

---

## Quick Start — Docker

### 1. Prepare `.env`

```env
PROJECT_ID=your-gee-project-id
SAVE_PATH=/data
TELEGRAM_TOKEN=your-telegram-bot-token
OLLAMA_MODEL=qwen2.5:7b
```

### 2. Build and run

```bash
docker-compose up --build -d
```

Open: **http://localhost:8080**

Results are saved to `./data/` on the host machine.

### 3. Stop

```bash
docker-compose down
```

---

## Telegram Bot

The bot starts automatically alongside the web app and provides real-time monitoring of your downloads.

### Setup

1. Create a bot via [@BotFather](https://t.me/BotFather) → copy the token
2. Add `TELEGRAM_TOKEN=<your-token>` to `.env`
3. Start the app — the bot goes live immediately
4. Open Telegram, find your bot, send `/start`

### Commands

| Command | Description |
|---|---|
| `/start` | Welcome message + auto-subscribe to notifications |
| `/status` | Quick overview: total jobs and count per status |
| `/jobs` | List 10 most recent jobs with inline Details / Logs buttons |
| `/logs <job_id>` | View the execution log for a specific job |
| `/cancel <job_id>` | Cancel a pending or running job |
| `/subscribe` | Enable automatic finish notifications |
| `/unsubscribe` | Disable notifications |

### AI Assistant (Ollama)

Any plain-language message is processed by a local Ollama model with tool calling.
The bot can access live database data to answer questions like:

- "How many downloads are there?"
- "Which jobs failed?"
- "Why did the last job fail?" *(reads the actual log)*
- "What are the current settings?"
- "Show me details for job abc123"

Notifications are sent automatically every 30 seconds when a job reaches `done`, `error`, or `cancelled` state.

---

## Ollama — Model Guide

[Ollama](https://ollama.com) runs AI models locally. The bot uses it for natural language understanding and tool calling.

### Install Ollama

```bash
# Linux
curl -fsSL https://ollama.com/install.sh | sh

# Pull a model
ollama pull qwen2.5:7b
```

### Model Comparison

| Model | Size on disk | RAM needed | Tool calling | Speed | Best for |
|---|---|---|---|---|---|
| `qwen2.5:7b` | 4.7 GB | ~6 GB | Excellent | Fast | **Recommended default** |
| `qwen2.5-coder:32b` | 19 GB | ~22 GB | Excellent | Moderate | Complex queries, code analysis |
| `qwen3:8b` | 5.2 GB | ~7 GB | Excellent | Fast | Latest Qwen generation |
| `qwen3:4b` | 2.6 GB | ~4 GB | Good | Very fast | Low-RAM machines |
| `llama3.2:3b` | 2.0 GB | ~3 GB | Good | Very fast | Minimal hardware |
| `llama3.1:8b` | 4.9 GB | ~6 GB | Good | Fast | General use |
| `mistral:7b` | 4.1 GB | ~5 GB | Good | Fast | General use |
| `phi4:14b` | 8.9 GB | ~11 GB | Good | Moderate | High quality, mid-size |

### Which model to choose

**You have `qwen2.5:7b` installed** — this is already a great choice. It handles tool calling (DB queries) reliably and responds quickly.

**If you want even better quality:** pull `qwen3:8b` and set `OLLAMA_MODEL=qwen3:8b` in `.env`.

**If your machine has limited RAM (< 6 GB free):** use `llama3.2:3b` or `qwen3:4b`.

**`qwen2.5-coder:32b`** (also installed) is overkill for chat queries but will give the most thoughtful and detailed answers — at the cost of slower response time.

### Change the model

Edit `.env`:
```env
OLLAMA_MODEL=qwen3:8b
```
The app reloads automatically (uvicorn `--reload` mode).

---

## Google Earth Engine Setup

1. Go to [https://earthengine.google.com](https://earthengine.google.com) → **Sign Up**
2. Sign in with a Google account and submit the registration form
3. Once approved, open [Google Cloud Console](https://console.cloud.google.com)
4. Create a project → enable **Earth Engine API**
5. Create a **Service Account**: IAM & Admin → Service Accounts → Create
   - Assign role **Earth Engine Resource Writer**
6. Create a JSON key for the Service Account → download the file
7. Register your project in EE: [https://code.earthengine.google.com/register](https://code.earthengine.google.com/register)

### Add GEE account in the app

1. Open **http://localhost:8080** → tab **Accounts**
2. Click **Add account**
3. Enter a name, select type `service_account`
4. Upload the downloaded JSON key
5. Click **Save** → set as **Default**

---

## Usage

### Downloading imagery

1. Tab **Download**
2. Drag and drop a GeoJSON file (or click to upload)
3. Select product: RGB / RGB+NIR / NDVI / Custom
4. Set date or date range:
   - **Single date**: `22.06.2024`
   - **Range**: `01.04.2024` → `01.10.2024`, step `4` (every 4 days)
   - **From GeoJSON**: date is taken from feature properties
5. Optionally enable **Single raster** (one bbox for all polygons)
6. Click **Run**

### Monitoring jobs

- Tab **Jobs** — list of all jobs with status
- **Log** button — expand/collapse execution log
- **Cancel** button — stop a running job

### Settings

Tab **Settings**:

| Parameter | Description | Default |
|---|---|---|
| `Project ID` | GEE Project ID | — |
| `Save Path` | Output path for TIF files | `/data` |
| `Scale (m)` | Pixel resolution in meters | `10` |
| `Max Cloud %` | Maximum cloud cover threshold | `20` |
| `Workers` | Parallel download threads | `4` |
| `Win Main (days)` | Main composite search window | `3` |
| `Win Fill (days)` | Gap fill search window | `15` |

---

## Project Structure

```
10_10/
├── 10_10.py            # Core: GEE composite, tile download, merge
├── app/
│   ├── main.py         # FastAPI routes + lifespan (starts bot)
│   ├── worker.py       # Background worker (threading)
│   ├── storage.py      # SQLite: jobs, accounts, settings, subscribers
│   └── bot.py          # Telegram bot + Ollama AI assistant
├── static/
│   └── index.html      # Web UI (Alpine.js + Tailwind CSS + Leaflet)
├── uploads/            # Temporary GeoJSON files
├── data/               # Download results (TIF, JPG)
├── .env                # Environment variables (token, model, paths)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── run.sh
```

---

## Output Structure

For each job a directory is created:

```
data/
└── S2_{filename}_{name}_{date}_{scale}m/
    ├── RGB_{filename}_{name}_{date}_{scale}m_merged.tif   # GeoTIFF (merged)
    ├── RGB_{filename}_{name}_{date}_{scale}m.jpg          # JPEG preview
    └── tiles/{date}/
        ├── RGB_c0_r0.tif
        ├── RGB_c1_r0.tif
        └── ...
```

---

## Environment Variables

All parameters can be set via `.env` or shell environment:

```env
# Google Earth Engine
PROJECT_ID=your-gee-project-id
SAVE_PATH=/data
SCALE_M=10
MAX_CLOUD_PERCENT=20
MAX_WORKERS=4
MAX_RETRIES=6
HTTP_TIMEOUT=300
BASE_SLEEP=2.0
MAX_SLEEP=120.0

# Telegram bot
TELEGRAM_TOKEN=your-telegram-bot-token
OLLAMA_MODEL=qwen2.5:7b
```

---

## Recommended Server Specs

| Parameter | Minimum | Recommended |
|---|---|---|
| CPU | 2 cores | 4–8 cores |
| RAM | 4 GB + model size | 16–32 GB |
| Disk | 50 GB SSD | 500 GB+ SSD |
| OS | Ubuntu 22.04 | Ubuntu 22.04 LTS |
| Network | 100 Mbps | 1 Gbps |

Note: add the Ollama model's RAM requirement on top (e.g. `qwen2.5:7b` needs ~6 GB).

---

## License

MIT

---
---

# Sentinel Hub Downloader (Русский)

Веб-приложение для автоматической пакетной загрузки снимков **Sentinel-2** через Google Earth Engine (GEE).
Поддерживает загрузку по полигонам GeoJSON, диапазоны дат, несколько спектральных продуктов и Telegram-бот с ИИ-ассистентом.

---

## Возможности

- Загрузка Sentinel-2 L2A снимков по полигонам из GeoJSON
- Режимы продуктов: **RGB**, **RGB+NIR**, **NDVI**, **Custom** (любые bands)
- Одна дата или диапазон дат с шагом (например, каждые 4 дня)
- Режим **Единый растр** — один bbox для всех полигонов (1 запрос вместо N)
- Облачная маска + composite-заполнение пропусков (fill-window)
- Параллельная загрузка тайлов с авто-retry и exponential backoff
- Несколько аккаунтов GEE (Service Account / User)
- Web-интерфейс с картой, drag-and-drop загрузкой, логами задач в реальном времени
- История задач и настройки в SQLite
- **Telegram-бот** с ИИ-ассистентом (Ollama) для мониторинга и уведомлений
- Поддержка Docker / docker-compose

---

## Быстрый старт — локально

### 1. Клонировать репозиторий

```bash
git clone <repo-url>
cd 10_10
```

### 2. Создать виртуальное окружение

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Системные зависимости (Debian/Ubuntu)

```bash
sudo apt-get install -y gdal-bin libgdal-dev libgeos-dev libproj-dev libspatialindex-dev build-essential
```

### 4. Python-зависимости

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 5. Настройка окружения

Создать файл `.env` в корне проекта:

```env
PROJECT_ID=your-gee-project-id
SAVE_PATH=/data

# Telegram-бот
TELEGRAM_TOKEN=токен-вашего-бота
OLLAMA_MODEL=qwen2.5:7b
```

### 6. Запустить приложение

```bash
bash run.sh
```

Открыть в браузере: **http://localhost:8080**

---

## Быстрый старт — Docker

```bash
# Настройте .env, затем:
docker-compose up --build -d
```

---

## Telegram-бот

Бот запускается автоматически вместе с веб-приложением.

### Настройка

1. Создать бота через [@BotFather](https://t.me/BotFather) → скопировать токен
2. Добавить `TELEGRAM_TOKEN=<токен>` в `.env`
3. Запустить приложение — бот сразу активен
4. Найти бота в Telegram, отправить `/start`

### Команды

| Команда | Описание |
|---|---|
| `/start` | Приветствие + автоподписка на уведомления |
| `/status` | Краткая сводка: всего задач и счётчик по статусам |
| `/jobs` | Список 10 последних задач с кнопками «Детали» / «Лог» |
| `/logs <job_id>` | Лог выполнения конкретной задачи |
| `/cancel <job_id>` | Отменить pending или running задачу |
| `/subscribe` | Включить уведомления о завершении задач |
| `/unsubscribe` | Отключить уведомления |

### ИИ-ассистент (Ollama)

Любое сообщение в свободной форме обрабатывается локальной языковой моделью через tool calling.
Бот обращается к базе данных в реальном времени и отвечает на вопросы:

- «Сколько всего загрузок?»
- «Какие задачи упали с ошибкой?»
- «Почему последняя задача завершилась с ошибкой?» *(читает реальный лог)*
- «Какие сейчас настройки?»
- «Покажи детали задачи abc123»

Уведомления приходят автоматически (каждые 30 секунд проверяется БД) при переходе задачи в статус `done`, `error` или `cancelled`.

---

## Ollama — выбор модели

[Ollama](https://ollama.com) запускает языковые модели локально. Бот использует их для понимания естественного языка и вызова инструментов.

### Установка Ollama

```bash
# Linux
curl -fsSL https://ollama.com/install.sh | sh

# Скачать модель
ollama pull qwen2.5:7b
```

### Сравнение моделей

| Модель | Размер | RAM | Tool calling | Скорость | Для чего |
|---|---|---|---|---|---|
| `qwen2.5:7b` | 4.7 ГБ | ~6 ГБ | Отлично | Быстро | **Рекомендуется по умолчанию** |
| `qwen2.5-coder:32b` | 19 ГБ | ~22 ГБ | Отлично | Умеренно | Сложные запросы, анализ кода |
| `qwen3:8b` | 5.2 ГБ | ~7 ГБ | Отлично | Быстро | Новейшее поколение Qwen |
| `qwen3:4b` | 2.6 ГБ | ~4 ГБ | Хорошо | Очень быстро | Машины с малым объёмом RAM |
| `llama3.2:3b` | 2.0 ГБ | ~3 ГБ | Хорошо | Очень быстро | Минимальное железо |
| `llama3.1:8b` | 4.9 ГБ | ~6 ГБ | Хорошо | Быстро | Общее использование |
| `mistral:7b` | 4.1 ГБ | ~5 ГБ | Хорошо | Быстро | Общее использование |
| `phi4:14b` | 8.9 ГБ | ~11 ГБ | Хорошо | Умеренно | Высокое качество, средний размер |

### Как выбрать

**Уже установлено `qwen2.5:7b`** — отличный выбор. Надёжно работает с tool calling и отвечает быстро.

**Хотите ещё лучше:** установите `qwen3:8b` и задайте `OLLAMA_MODEL=qwen3:8b` в `.env`.

**Мало RAM (< 6 ГБ свободно):** используйте `llama3.2:3b` или `qwen3:4b`.

**`qwen2.5-coder:32b`** (уже установлена) — избыточна для чат-запросов, но даёт самые развёрнутые и точные ответы. Медленнее остальных.

### Смена модели

Отредактировать `.env`:
```env
OLLAMA_MODEL=qwen3:8b
```
Приложение перезагрузится автоматически (uvicorn `--reload`).

---

## Регистрация Google Earth Engine

1. [https://earthengine.google.com](https://earthengine.google.com) → **Sign Up**
2. Войти через Google-аккаунт, заполнить форму
3. После одобрения — [Google Cloud Console](https://console.cloud.google.com)
4. Создать проект → включить **Earth Engine API**
5. IAM & Admin → Service Accounts → Create → роль **Earth Engine Resource Writer**
6. Создать JSON-ключ → скачать файл
7. Зарегистрировать проект: [https://code.earthengine.google.com/register](https://code.earthengine.google.com/register)

### Добавление аккаунта в приложении

1. **http://localhost:8080** → вкладка **Аккаунты**
2. **Добавить аккаунт** → ввести имя, тип `service_account`
3. Загрузить JSON-ключ → **Сохранить** → **По умолчанию**

---

## Использование

### Загрузка снимка

1. Вкладка **Загрузка**
2. Перетащить GeoJSON-файл (или выбрать)
3. Выбрать продукт: RGB / RGB+NIR / NDVI / Custom
4. Задать дату или диапазон:
   - **Одна дата**: `22.06.2024`
   - **Диапазон**: `01.04.2024` → `01.10.2024`, шаг `4`
   - **Из GeoJSON**: дата берётся из свойств фич
5. Опционально: **Единый растр**
6. **Запустить**

### Мониторинг

- Вкладка **Задачи** — список с статусами
- **Лог** — раскрыть журнал выполнения
- **Отмена** — остановить задачу

### Настройки

| Параметр | Описание | По умолчанию |
|---|---|---|
| `Project ID` | GEE Project ID | — |
| `Save Path` | Путь для TIF | `/data` |
| `Scale (m)` | Разрешение пикселя в метрах | `10` |
| `Max Cloud %` | Макс. облачность | `20` |
| `Workers` | Параллельных потоков | `4` |
| `Win Main (days)` | Окно главного composite | `3` |
| `Win Fill (days)` | Окно заполнения пропусков | `15` |

---

## Структура проекта

```
10_10/
├── 10_10.py            # Ядро: GEE composite, загрузка тайлов, merge
├── app/
│   ├── main.py         # FastAPI роуты + lifespan (запуск бота)
│   ├── worker.py       # Фоновый воркер (threading)
│   ├── storage.py      # SQLite: задачи, аккаунты, настройки, подписчики
│   └── bot.py          # Telegram-бот + Ollama ИИ-ассистент
├── static/
│   └── index.html      # Web-интерфейс (Alpine.js + Tailwind + Leaflet)
├── uploads/            # Временные GeoJSON файлы
├── data/               # Результаты (TIF, JPG)
├── .env                # Переменные окружения
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── run.sh
```

---

## Переменные окружения

```env
# Google Earth Engine
PROJECT_ID=your-gee-project-id
SAVE_PATH=/data
SCALE_M=10
MAX_CLOUD_PERCENT=20
MAX_WORKERS=4
MAX_RETRIES=6
HTTP_TIMEOUT=300
BASE_SLEEP=2.0
MAX_SLEEP=120.0

# Telegram-бот
TELEGRAM_TOKEN=токен-вашего-бота
OLLAMA_MODEL=qwen2.5:7b
```

---

## Рекомендуемые характеристики сервера

| Параметр | Минимум | Рекомендуется |
|---|---|---|
| CPU | 2 ядра | 4–8 ядер |
| RAM | 4 ГБ + размер модели | 16–32 ГБ |
| Диск | 50 ГБ SSD | 500 ГБ+ SSD |
| ОС | Ubuntu 22.04 | Ubuntu 22.04 LTS |
| Сеть | 100 Мбит/с | 1 Гбит/с |

Примечание: к RAM прибавьте требования модели Ollama (например, `qwen2.5:7b` — ~6 ГБ).

---

## Лицензия

MIT
