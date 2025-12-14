import os
import time
from datetime import date, datetime, timedelta

import ee
import requests
from dotenv import load_dotenv

load_dotenv()

PROJECT_ID = os.getenv("PROJECT_ID")

LEFT_LON = float(os.getenv("LEFT_LON"))
RIGHT_LON = float(os.getenv("RIGHT_LON"))
BOTTOM_LAT = float(os.getenv("BOTTOM_LAT"))
TOP_LAT = float(os.getenv("TOP_LAT"))

SAVE_PATH = os.getenv("SAVE_PATH")


TEMP_DIR = os.path.join(SAVE_PATH, "temperature")
os.makedirs(TEMP_DIR, exist_ok=True)


COLLECTION_ID = "ECMWF/ERA5_LAND/HOURLY"
BANDS = ["temperature_2m"]
SCALE_METERS = 11132
CRS = "EPSG:4326"


ee.Initialize(project=PROJECT_ID)

region_geom = ee.Geometry.Rectangle([LEFT_LON, BOTTOM_LAT, RIGHT_LON, TOP_LAT])


def get_last_downloaded_day():
    dates = set()

    for f in os.listdir(TEMP_DIR):
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


def download_era5_day(day: date):
    print(f"\n=== 🌡 Температура {day} ===")

    start = ee.Date(str(day))
    end = ee.Date(str(day + timedelta(days=1)))

    col = (
        ee.ImageCollection(COLLECTION_ID)
        .filterDate(start, end)
        .filterBounds(region_geom)
        .select(BANDS)
        .sort("system:time_start")
    )

    count = col.size().getInfo()
    if count == 0:
        print("  ⚠ Нет данных")
        return

    images = col.toList(count)
    date_str = day.strftime("%Y%m%d")

    for i in range(count):
        img = ee.Image(images.get(i))
        hour = ee.Date(img.get("system:time_start")).format("HH").getInfo()

        filename = f"{date_str}_{hour}_ERA_temp.tif"
        filepath = os.path.join(TEMP_DIR, filename)

        if os.path.exists(filepath):
            continue

        print(f"  ⬇ {filename}")

        img_c = img.select("temperature_2m").subtract(273.15).rename("temp_C")

        try:
            url = img_c.getDownloadURL(
                {
                    "scale": SCALE_METERS,
                    "region": region_geom,
                    "crs": CRS,
                    "format": "GEO_TIFF",
                }
            )
            r = requests.get(url, stream=True, timeout=120)
            r.raise_for_status()
        except Exception as e:
            print(f"    ❌ Ошибка: {e}")
            return

        with open(filepath, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)

        print("    ✔ OK")


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
        download_era5_day(day)
        day += timedelta(days=1)


if __name__ == "__main__":
    print("🟢 Автоскачивание температуры запущено (00:00 UTC)")

    while True:
        sleep_until_midnight_utc()
        try:
            run_sync()
        except Exception as e:
            print(f"🔥 Критическая ошибка: {e}")
