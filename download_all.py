import os
from dotenv import load_dotenv
import ee
import requests

# ---- Загружаем ENV ----
load_dotenv()

PROJECT_ID = os.getenv("PROJECT_ID")

START_DATE = os.getenv("START_DATE")
END_DATE   = os.getenv("END_DATE")

LEFT_LON   = float(os.getenv("LEFT_LON"))
RIGHT_LON  = float(os.getenv("RIGHT_LON"))
BOTTOM_LAT = float(os.getenv("BOTTOM_LAT"))
TOP_LAT    = float(os.getenv("TOP_LAT"))

SAVE_PATH = os.getenv("SAVE_PATH")


# ---- Инициализация Earth Engine ----
ee.Initialize(project=PROJECT_ID)

print(f"Будут скачаны данные за период: {START_DATE} → {END_DATE}")

# ---- Регион ----
region = ee.Geometry.Rectangle([LEFT_LON, BOTTOM_LAT, RIGHT_LON, TOP_LAT])


# ---- Датасеты ----
gases = {
    "CH4":  ("COPERNICUS/S5P/OFFL/L3_CH4",  "CH4_column_volume_mixing_ratio_dry_air"),
    "CO":   ("COPERNICUS/S5P/OFFL/L3_CO",   "CO_column_number_density"),
    "NO2":  ("COPERNICUS/S5P/OFFL/L3_NO2",  "NO2_column_number_density"),
    "SO2":  ("COPERNICUS/S5P/OFFL/L3_SO2",  "SO2_column_number_density"),
    "O3":   ("COPERNICUS/S5P/OFFL/L3_O3",   "O3_column_number_density"),
    "HCHO": ("COPERNICUS/S5P/OFFL/L3_HCHO", "tropospheric_HCHO_column_number_density"),
    "AERAI":("COPERNICUS/S5P/OFFL/L3_AER_AI","absorbing_aerosol_index")
}


# ---- Основной цикл скачивания ----
for gas_name, (dataset, band) in gases.items():

    print(f"\n=== Обрабатываю {gas_name} ===")

    col = (
        ee.ImageCollection(dataset)
        .select(band)
        .filterDate(START_DATE, END_DATE)
    )

    img = col.mean().clip(region)

    url = img.getDownloadURL({
        "scale": 7000,
        "region": region,
        "format": "GEO_TIFF"
    })

    outfile = f"{SAVE_PATH}/{gas_name}_{START_DATE}.tif"
    print(f"⬇ Скачиваю в {outfile}")

    r = requests.get(url)
    with open(outfile, "wb") as f:
        f.write(r.content)

    print(f"✔ Сохранено: {outfile}")

print("\n🎉 Все TIFF сохранены локально!")
