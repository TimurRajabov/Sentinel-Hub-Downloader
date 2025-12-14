import os
import time
from datetime import date, datetime, timedelta

import ee
import requests
from dotenv import load_dotenv

# ---------- LOAD ENV ----------
load_dotenv()

PROJECT_ID = os.getenv("PROJECT_ID")

LEFT_LON = float(os.getenv("LEFT_LON"))
RIGHT_LON = float(os.getenv("RIGHT_LON"))
BOTTOM_LAT = float(os.getenv("BOTTOM_LAT"))
TOP_LAT = float(os.getenv("TOP_LAT"))

SAVE_PATH = os.getenv("SAVE_PATH")

# ---------- OUTPUT ----------
OUTPUT_DIR = os.path.join(SAVE_PATH, "wind")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------- CONSTANTS ----------
COLLECTION_ID = "ECMWF/ERA5/HOURLY"
BANDS = ["u_component_of_wind_10m", "v_component_of_wind_10m"]
SCALE_METERS = 27827
CRS = "EPSG:4326"

# ---------- INIT GEE ----------
ee.Initialize(project=PROJECT_ID)

region_geom = ee.Geometry.Rectangle([LEFT_LON, BOTTOM_LAT, RIGHT_LON, TOP_LAT])


# ---------- HELPERS ----------
def get_last_downloaded_day():
    dates = set()

    for f in os.listdir(OUTPUT_DIR):
        try:
            d = f.split("_")[0]
            dates.add(datetime.strptime(d, "%Y%m%d").date())
        except:
            pass

    return max(dates) if dates else None


def sleep_until_midnight_utc():
    now = datetime.utcnow()
    next_midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    seconds = (next_midnight - now).total_seconds()
    print(f"\n😴 Ждём до 00:00 UTC ({int(seconds)} сек)")
    time.sleep(seconds)


# -------------------------------------------------
#   BUILDER: СУТОЧНАЯ КОЛЛЕКЦИЯ ВЕТРА
# -------------------------------------------------
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


# -------------------------------------------------
#   СКАЧИВАНИЕ ЗА ДЕНЬ
# -------------------------------------------------
def download_era5_for_day(day_py: date):
    print(f"\n=== 🌬 Ветер {day_py} ===")

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
                {
                    "scale": SCALE_METERS,
                    "region": region_geom,
                    "crs": CRS,
                    "format": "GEO_TIFF",
                }
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


# -------------------------------------------------
#   SYNC LOGIC
# -------------------------------------------------
def run_sync():
    today = datetime.utcnow().date()
    last_available_day = today - timedelta(days=1)

    last_day = get_last_downloaded_day()

    if last_day:
        day = last_day + timedelta(days=1)
        print(f"▶ Продолжаем с {day}")
    else:
        day = last_available_day
        print("▶ Нет файлов — стартуем с последнего дня")

    while day <= last_available_day:
        download_era5_for_day(day)
        day += timedelta(days=1)


# -------------------------------------------------
#   AUTO MODE (00:00 UTC)
# -------------------------------------------------
if __name__ == "__main__":
    print("🟢 Автоскачивание ветра запущено (00:00 UTC)")

    while True:
        sleep_until_midnight_utc()
        try:
            run_sync()
        except Exception as e:
            print(f"🔥 Критическая ошибка: {e}")
