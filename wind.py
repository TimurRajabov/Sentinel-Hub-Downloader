import os
import random
import time
from datetime import date, datetime, timedelta

import ee  # type: ignore
import requests
from dotenv import load_dotenv
from requests.exceptions import RequestException, Timeout

load_dotenv()

PROJECT_ID = os.getenv("PROJECT_ID")

LEFT_LON = float(os.getenv("LEFT_LON"))
RIGHT_LON = float(os.getenv("RIGHT_LON"))
BOTTOM_LAT = float(os.getenv("BOTTOM_LAT"))
TOP_LAT = float(os.getenv("TOP_LAT"))

SAVE_PATH = os.getenv("SAVE_PATH", "/data")

START_DATE = os.getenv("START_DATE")
END_DATE = os.getenv("END_DATE")

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "6"))
BASE_SLEEP = float(os.getenv("BASE_SLEEP", "2.0"))
MAX_SLEEP = float(os.getenv("MAX_SLEEP", "120.0"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "300"))

SKIP_DAY_ON_FAIL = os.getenv("SKIP_DAY_ON_FAIL", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
    "on",
)

COOLDOWN_HOURS_ON_FAILURE = float(os.getenv("COOLDOWN_HOURS_ON_FAILURE", "5"))
SLEEP_BETWEEN_PASSES_SEC = int(os.getenv("SLEEP_BETWEEN_PASSES_SEC", "300"))


def parse_day(s: str | None) -> date | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d").date()


START_DAY = parse_day(START_DATE)
END_DAY = parse_day(END_DATE)

if START_DAY is None or END_DAY is None:
    raise RuntimeError("В .env нужно задать START_DATE и END_DATE в формате YYYY-MM-DD")
if START_DAY > END_DAY:
    raise RuntimeError("START_DATE не может быть больше END_DATE")
if not PROJECT_ID:
    raise RuntimeError("PROJECT_ID is not set in .env")


COLLECTION_ID = "ECMWF/ERA5_LAND/HOURLY"
BAND = "temperature_2m"
SCALE_METERS = 11132
CRS = "EPSG:4326"

OUTPUT_DIR = os.path.join(SAVE_PATH, "temperature")
os.makedirs(OUTPUT_DIR, exist_ok=True)

ee.Initialize(project=PROJECT_ID)
region_geom = ee.Geometry.Rectangle([LEFT_LON, BOTTOM_LAT, RIGHT_LON, TOP_LAT])


def _backoff_sleep(attempt: int) -> None:
    sleep_s = min(MAX_SLEEP, BASE_SLEEP * (2 ** (attempt - 1))) + random.random()
    print(f"backoff {sleep_s:.1f} сек...")
    time.sleep(sleep_s)


def download_with_retries(img: ee.Image, outfile: str) -> None:
    params = {
        "scale": SCALE_METERS,
        "region": region_geom,
        "crs": CRS,
        "format": "GEO_TIFF",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            url = img.getDownloadURL(params)
            with requests.get(url, stream=True, timeout=HTTP_TIMEOUT) as r:
                if r.status_code in (429, 500, 502, 503, 504):
                    raise RequestException(f"{r.status_code} {r.reason} for url: {url}")
                r.raise_for_status()

                tmp = outfile + ".part"
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1024 * 1024):
                        if chunk:
                            f.write(chunk)
                os.replace(tmp, outfile)
            return

        except (RequestException, Timeout) as e:
            print(f"сеть/сервис (попытка {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                _backoff_sleep(attempt)
                continue
            raise

        except Exception as e:
            print(f"ошибка (попытка {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                _backoff_sleep(attempt)
                continue
            raise


def get_last_downloaded_day() -> date | None:
    dates = set()
    for f in os.listdir(OUTPUT_DIR):

        try:
            d = f.split("_")[0]
            dates.add(datetime.strptime(d, "%Y%m%d").date())
        except Exception:
            pass
    return max(dates) if dates else None


def list_images_for_day(day_py: date):
    start = ee.Date(day_py.isoformat())
    end = start.advance(1, "day")

    col = (
        ee.ImageCollection(COLLECTION_ID)
        .filterDate(start, end)
        .filterBounds(region_geom)
        .select([BAND])
        .sort("system:time_start")
    )

    count = col.size().getInfo()
    if count == 0:
        return None, 0

    return col.toList(count), count


def download_temp_for_day(day_py: date) -> bool:
    print(f"\n=== 🌡 TEMP {day_py} (UTC) ===")

    images_list, count = list_images_for_day(day_py)
    if images_list is None or count == 0:
        print(f"  ⚠ Нет данных ERA5-Land за {day_py.isoformat()}")
        return False

    print(f"  Найдено {count} снимков (обычно ~24).")

    had_failures = False
    date_str = day_py.strftime("%Y%m%d")

    for idx in range(count):
        img = ee.Image(images_list.get(idx)).select(BAND)

        hour_str = ee.Date(img.get("system:time_start")).format("HH").getInfo()

        tif_name = f"{date_str}_{hour_str}_ERA_temp.tif"
        out_path = os.path.join(OUTPUT_DIR, tif_name)

        if os.path.exists(out_path):
            continue

        print(f"  ⬇ {tif_name.replace('.tif','')}")

        img_c = img.subtract(273.15).rename("temp")

        try:
            download_with_retries(img_c, out_path)
            print("    ✔ OK")
        except Exception as e:
            had_failures = True
            print(f"    ❌ Не удалось скачать {tif_name}: {e}")

    return had_failures


def run_sync() -> bool:
    had_failures = False
    print(f"\n🚀 Синхронизация TEMP: {START_DAY} .. {END_DAY} | SAVE_PATH={SAVE_PATH}")

    last_day = get_last_downloaded_day()
    if last_day:
        day = max(last_day + timedelta(days=1), START_DAY)
        print(f"▶ Продолжаем с {day} (последний файл был {last_day})")
    else:
        day = START_DAY
        print(f"▶ Нет файлов — стартуем с {START_DAY}")

    if day > END_DAY:
        print("✅ Уже всё скачано в заданном диапазоне")
        return False

    while day <= END_DAY:
        day_failed = download_temp_for_day(day)

        if day_failed:
            had_failures = True
            if SKIP_DAY_ON_FAIL:
                print(
                    "  ⚠ Были ошибки за день, продолжаем следующий (SKIP_DAY_ON_FAIL=1)."
                )
            else:
                print("  🛑 Были ошибки за день, останавливаемся (SKIP_DAY_ON_FAIL=0).")
                break

        day += timedelta(days=1)

    return had_failures


if __name__ == "__main__":
    print(f"🟢 Вечный режим TEMP: диапазон={START_DAY}..{END_DAY}")

    while True:
        try:
            had_failures = run_sync()
        except Exception as e:
            print(f"🔥 Критическая ошибка в run_sync(): {e}")
            had_failures = True

        if had_failures:
            sleep_s = int(COOLDOWN_HOURS_ON_FAILURE * 3600)
            print(
                f"🛌 Были ошибки. Повторим через {COOLDOWN_HOURS_ON_FAILURE} часов ({sleep_s} сек)"
            )
            time.sleep(sleep_s)
        else:
            print(f"✅ Проход завершён. Пауза {SLEEP_BETWEEN_PASSES_SEC} сек")
            time.sleep(SLEEP_BETWEEN_PASSES_SEC)
