import os
from datetime import date, timedelta

import requests
import ee

# ---------- АВТОРИЗАЦИЯ GEE ----------
ee.Authenticate()
ee.Initialize(project='sapient-depot-457604-e2')  # <-- при необходимости поменяй проект


# ---------- НАСТРОЙКИ ----------
ASSET_ID = 'projects/steel-league-457811-i1/assets/TEST2'   # ROI как в JS
OUTPUT_DIR = "/home/temur/Documents/Work/goo/test"              # куда сохраняем

# Месяц как в JS-примере: октябрь 2025
START_DAY = date(2025, 10, 1)
NUM_DAYS = 31   # для октября 31 день

COLLECTION_ID = 'COPERNICUS/S5P/NRTI/L3_O3'
BAND_NAME = 'O3_column_number_density'

SCALE_METERS = 1113          # как в твоём JS Export.image.toDrive
CRS = "EPSG:4326"            # географические координаты

# ---------- ПОДГОТОВКА ПАПКИ ----------
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------- ГЕОМЕТРИЯ РЕГИОНА ----------
region_fc = ee.FeatureCollection(ASSET_ID)
region_geom = region_fc.geometry()


def download_o3_for_day(day: date):
    """
    Качает среднесуточную карту O3 за один день и сохраняет GeoTIFF.
    Аналог createDailyImage + Export.image.toDrive, но только за 1 дату.
    """
    print(f"\n=== Обработка даты {day} ===")

    start = ee.Date(str(day))
    end = ee.Date(str(day + timedelta(days=1)))
    date_str = day.strftime("%Y%m%d")

    # Коллекция O3 за сутки
    daily_collection = (
        ee.ImageCollection(COLLECTION_ID)
        .filterDate(start, end)
        .filterBounds(region_geom)
        .select(BAND_NAME)
    )

    # Проверка, есть ли вообще данные за этот день
    count = daily_collection.size().getInfo()
    if count == 0:
        print("  ⚠ Нет данных за этот день, пропускаем.")
        return

    # Среднее за сутки
    daily_img = daily_collection.mean().rename(f"O3_{date_str}")
    daily_img = daily_img.clip(region_geom)

    # Локальный путь для сохранения
    out_path = os.path.join(OUTPUT_DIR, f"O3_{date_str}.tif")

    print("  -> Получаем download URL из GEE...")
    url = daily_img.getDownloadURL({
        "scale": SCALE_METERS,
        "region": region_geom,
        "crs": CRS,
        "format": "GEO_TIFF"
    })

    print("  Download URL:", url)
    print(f"  -> Качаем в файл: {out_path}")

    resp = requests.get(url, stream=True)
    resp.raise_for_status()

    with open(out_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)

    print("  ✅ Готово.")


def main():
    for i in range(NUM_DAYS):
        day = START_DAY + timedelta(days=i)
        try:
            download_o3_for_day(day)
        except Exception as e:
            print(f"  ❌ Ошибка для {day}: {e}")


if __name__ == "__main__":
    main()