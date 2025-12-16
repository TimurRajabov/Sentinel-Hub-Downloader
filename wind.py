import os
import time
from datetime import date, datetime, timedelta

import ee
import requests
from dotenv import load_dotenv

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:
    raise RuntimeError("Нужен Python 3.9+ (zoneinfo)")


load_dotenv()

PROJECT_ID = os.getenv("PROJECT_ID")

LEFT_LON = float(os.getenv("LEFT_LON"))
RIGHT_LON = float(os.getenv("RIGHT_LON"))
BOTTOM_LAT = float(os.getenv("BOTTOM_LAT"))
TOP_LAT = float(os.getenv("TOP_LAT"))

SAVE_PATH = os.getenv("SAVE_PATH", "/data")

RUN_AT = os.getenv("RUN_AT", "00:00")
RUN_TZ = os.getenv("RUN_TZ", "UTC")

START_DATE = os.getenv("START_DATE")
END_DATE = os.getenv("END_DATE")


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


OUTPUT_DIR = os.path.join(SAVE_PATH, "wind")
os.makedirs(OUTPUT_DIR, exist_ok=True)

COLLECTION_ID = "ECMWF/ERA5/HOURLY"
BANDS = ["u_component_of_wind_10m", "v_component_of_wind_10m"]
SCALE_METERS = 27827
CRS = "EPSG:4326"

ee.Initialize(project=PROJECT_ID)
region_geom = ee.Geometry.Rectangle([LEFT_LON, BOTTOM_LAT, RIGHT_LON, TOP_LAT])


def sleep_until_scheduled_time():
    tz = ZoneInfo(RUN_TZ)
    now = datetime.now(tz)

    hour, minute = map(int, RUN_AT.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)

    seconds = (target - now).total_seconds()
    print(f"\n😴 Ждём до {target.strftime('%Y-%m-%d %H:%M %Z')} ({int(seconds)} сек)")
    time.sleep(seconds)


def get_last_downloaded_day():
    dates = set()
    for f in os.listdir(OUTPUT_DIR):
        try:
            d = f.split("_")[0]
            dates.add(datetime.strptime(d, "%Y%m%d").date())
        except Exception:
            pass
    return max(dates) if dates else None


def build_image_and_mapping(day_py: date):
    day_iso = day_py.isoformat()
    date_str = day_py.strftime("%Y%m%d")

    start = ee.Date(day_iso)
    end = start.advance(1, "day")

    era5 = (
        ee.ImageCollection(COLLECTION_ID)
        .filterDate(start, end)
        .filterBounds(region_geom)
        .select(BANDS)
        .sort("system:time_start")
    )

    count = era5.size().getInfo()
    if count == 0:
        print(f"  ⚠ Нет данных ERA5 за {day_iso}")
        return None, None

    img = era5.toBands()
    orig_band_names = img.bandNames().getInfo()

    mapping = []
    hour = 0
    seen_u = False

    for b in orig_band_names:
        if "u_component_of_wind_10m" in b:
            new_name = f"{date_str}_U_{hour:02d}"
            mapping.append((b, new_name))
            seen_u = True
        elif "v_component_of_wind_10m" in b:
            new_name = f"{date_str}_V_{hour:02d}"
            mapping.append((b, new_name))
            if seen_u:
                hour += 1
                seen_u = False

    print(f"  Каналов найдено: {len(mapping)} (ожидается 48)")
    return img, mapping


def download_era5_for_day(day_py: date):
    print(f"\n=== 🌬 Ветер {day_py} (UTC-данные) ===")

    img, mapping = build_image_and_mapping(day_py)
    if img is None:
        return

    for old_band, new_band in mapping:
        out_path = os.path.join(OUTPUT_DIR, f"{new_band}.tif")

        if os.path.exists(out_path):
            continue

        print(f"  ⬇ {new_band}.tif")

        single_img = img.select([old_band]).rename(new_band)

        try:
            url = single_img.getDownloadURL(
                {"scale": SCALE_METERS, "region": region_geom, "crs": CRS, "format": "GEO_TIFF"}
            )
            resp = requests.get(url, stream=True, timeout=120)
            resp.raise_for_status()
        except Exception as e:
            print(f"    ❌ Ошибка: {e}")
            return

        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(8192):
                if chunk:
                    f.write(chunk)

        print("    ✔ OK")


def run_sync():
    print(f"\n🚀 Синхронизация ветра в диапазоне: {START_DAY} .. {END_DAY}")

    last_day = get_last_downloaded_day()

    if last_day:
        day = max(last_day + timedelta(days=1), START_DAY)
        print(f"▶ Продолжаем с {day} (последний файл был {last_day})")
    else:
        day = START_DAY
        print(f"▶ Нет файлов — стартуем с START_DATE={START_DAY}")

    if day > END_DAY:
        print("✅ Уже всё скачано в заданном диапазоне")
        return

    while day <= END_DAY:
        download_era5_for_day(day)
        day += timedelta(days=1)


if __name__ == "__main__":
    print(f"🟢 Автоскачивание ветра (RUN_AT={RUN_AT}, RUN_TZ={RUN_TZ}) диапазон={START_DAY}..{END_DAY}")

    while True:
        sleep_until_scheduled_time()
        try:
            run_sync()
        except Exception as e:
            print(f"🔥 Критическая ошибка: {e}")
