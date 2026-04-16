# Sentinel Hub Downloader

Web-приложение для автоматической загрузки снимков **Sentinel-2** через Google Earth Engine (GEE).  
Поддерживает пакетную загрузку по полигонам GeoJSON, диапазоны дат, несколько спектральных продуктов и режим «единого растра».

---

## Возможности

- Загрузка Sentinel-2 L2A снимков по полигонам из GeoJSON
- Режимы продуктов: **RGB**, **RGB+NIR**, **NDVI**, **Custom** (любые bands)
- Одиночная дата, диапазон дат с шагом (например, каждые 4 дня)
- Режим **Единый растр** — один bbox на все полигоны (1 запрос вместо N)
- Облачная маска + composite-заполнение пропусков (fill-window)
- Параллельная загрузка тайлов (ThreadPoolExecutor)
- Авто-retry с exponential backoff
- Управление несколькими аккаунтами GEE (Service Account / User)
- Web-интерфейс с картой, drag-and-drop загрузкой, логами задач
- Хранение настроек и истории задач в SQLite
- Docker / docker-compose поддержка

---

## Требования

| Зависимость | Версия |
|---|---|
| Python | 3.10+ |
| GDAL / rasterio | системная GDAL ≥ 3.x |
| Google Earth Engine account | см. ниже |

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

### 3. Установить системные зависимости (Debian/Ubuntu)

```bash
sudo apt-get install -y gdal-bin libgdal-dev libgeos-dev libproj-dev libspatialindex-dev build-essential
```

### 4. Установить Python-зависимости

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 5. Запустить приложение

```bash
bash run.sh
# или
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Открыть в браузере: **http://localhost:8080**

---

## Быстрый старт — Docker

### 1. Подготовить `.env` файл

```bash
cp .env.example .env   # или создать вручную
```

Пример `.env`:
```env
PROJECT_ID=your-gee-project-id
SAVE_PATH=/data
```

### 2. Собрать и запустить

```bash
docker-compose up --build -d
```

Открыть: **http://localhost:8080**

Результаты загрузки сохраняются в `./data/` на хост-машине.

### 3. Остановить

```bash
docker-compose down
```

---

## Регистрация Google Earth Engine

1. Перейти на [https://earthengine.google.com](https://earthengine.google.com) → **Sign Up**
2. Войти под Google-аккаунтом и заполнить форму заявки
3. После одобрения открыть [Google Cloud Console](https://console.cloud.google.com)
4. Создать проект → включить **Earth Engine API**
5. Создать **Service Account**: IAM & Admin → Service Accounts → Create
   - Назначить роль **Earth Engine Resource Writer**
6. Создать JSON-ключ для Service Account → скачать файл
7. Зарегистрировать проект в EE: [https://code.earthengine.google.com/register](https://code.earthengine.google.com/register)

---

## Добавление аккаунта GEE в приложении

1. Открыть **http://localhost:8080** → вкладка **Аккаунты**
2. Нажать **Добавить аккаунт**
3. Ввести имя, выбрать тип `service_account`
4. Загрузить скачанный JSON-ключ
5. Нажать **Сохранить** → установить как **По умолчанию**

---

## Использование

### Загрузка снимка

1. Вкладка **Загрузка**
2. Перетащить GeoJSON-файл в зону загрузки (или выбрать файл)
3. Выбрать продукт (RGB / RGB+NIR / NDVI / Custom)
4. Задать дату или диапазон дат:
   - **Одна дата**: `22.06.2024`
   - **Диапазон**: `01.04.2024` → `01.10.2024`, шаг `4` (каждые 4 дня)
   - **Из GeoJSON**: дата берётся из свойств фич
5. Опционально включить **Единый растр** (один bbox для всех полигонов)
6. Нажать **Запустить**

### Мониторинг задач

- Вкладка **Задачи** — список всех задач с статусом
- Кнопка **Лог** — раскрыть/свернуть журнал выполнения
- Кнопка **Отмена** — остановить выполняющуюся задачу

### Настройки

Вкладка **Настройки**:

| Параметр | Описание | По умолчанию |
|---|---|---|
| `Project ID` | GEE Project ID | — |
| `Save Path` | Путь для сохранения TIF | `/data` |
| `Scale (m)` | Разрешение пикселя в метрах | `10` |
| `Max Cloud %` | Макс. облачность снимка | `20` |
| `Workers` | Параллельных загрузчиков | `4` |
| `Win Main (days)` | Окно поиска главного composite | `3` |
| `Win Fill (days)` | Окно заполнения пропусков | `15` |

---

## Структура проекта

```
10_10/
├── 10_10.py            # Ядро: GEE composite, загрузка тайлов, merge
├── app/
│   ├── main.py         # FastAPI роуты
│   ├── worker.py       # Фоновый воркер (threading)
│   └── storage.py      # SQLite: задачи, аккаунты, настройки
├── static/
│   └── index.html      # Web-интерфейс (Alpine.js + Tailwind)
├── uploads/            # Временные GeoJSON файлы
├── data/               # Результаты загрузки (TIF, JPG)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── run.sh
```

---

## Выходные данные

Для каждой задачи создаётся директория:

```
data/
└── S2_{filename}_{name}_{date}_{scale}m/
    ├── RGB_{filename}_{name}_{date}_{scale}m_merged.tif   # GeoTIFF
    ├── RGB_{filename}_{name}_{date}_{scale}m.jpg          # Превью
    └── tiles/{date}/
        ├── RGB_c0_r0.tif
        ├── RGB_c1_r0.tif
        └── ...
```

---

## Переменные окружения

Все параметры можно задать через `.env` или переменные окружения:

```env
PROJECT_ID=your-gee-project-id
SAVE_PATH=/data
SCALE_M=10
MAX_CLOUD_PERCENT=20
MAX_WORKERS=4
MAX_RETRIES=6
HTTP_TIMEOUT=300
BASE_SLEEP=2.0
MAX_SLEEP=120.0
```

---

## Рекомендуемые характеристики сервера

| Параметр | Минимум | Рекомендуется |
|---|---|---|
| CPU | 2 ядра | 4–8 ядер |
| RAM | 4 GB | 8–16 GB |
| Диск | 50 GB SSD | 500 GB+ SSD |
| ОС | Ubuntu 22.04 | Ubuntu 22.04 LTS |
| Сеть | 100 Мбит/с | 1 Гбит/с |

---

## Лицензия

MIT
# Sentinel-Hub-Downloader
