import os
import time
from datetime import datetime, timedelta

import ee  # type: ignore
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

# ---------- INIT EE ----------
ee.Initialize(project=PROJECT_ID)

region = ee.Geometry.Rectangle([LEFT_LON, BOTTOM_LAT, RIGHT_LON, TOP_LAT])

# ---------- DATASETS ----------
gases = {
    "CH4": ("COPERNICUS/S5P/OFFL/L3_CH4", "CH4_column_volume_mixing_ratio_dry_air"),
    "CO": ("COPERNICUS/S5P/OFFL/L3_CO", "CO_column_number_density"),
    "NO2": ("COPERNICUS/S5P/OFFL/L3_NO2", "NO2_column_number_density"),
    "SO2": ("COPERNICUS/S5P/OFFL/L3_SO2", "SO2_column_number_density"),
    "O3": ("COPERNICUS/S5P/OFFL/L3_O3", "O3_column_number_density"),
    "HCHO": ("COPERNICUS/S5P/OFFL/L3_HCHO", "tropospheric_HCHO_column_number_density"),
    "AERAI": ("COPERNICUS/S5P/OFFL/L3_AER_AI", "absorbing_aerosol_index"),
}


# ---------- HELPERS ----------
def get_last_downloaded_date(gas_name):
    folder = os.path.join(SAVE_PATH, gas_name)
    if not os.path.exists(folder):
        return None

    dates = []
    for f in os.listdir(folder):
        try:
            d = f.split("_")[1].replace(".tif", "")
            dates.append(datetime.strptime(d, "%Y-%m-%d").date())
        except:
            pass

    return max(dates) if dates else None


def sleep_until_midnight_utc():
    now = datetime.utcnow()
    next_midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    seconds = (next_midnight - now).total_seconds()
    print(f"\n😴 Спим до 00:00 UTC ({int(seconds)} сек)")
    time.sleep(seconds)


# ---------- MAIN LOGIC ----------
def run_sync():
    today = datetime.utcnow().date()
    last_available_day = today - timedelta(days=1)

    print(f"\n🚀 Синхронизация. Доступно до {last_available_day}")

    for gas_name, (dataset, band) in gases.items():
        print(f"\n🧪 Газ: {gas_name}")

        gas_folder = os.path.join(SAVE_PATH, gas_name)
        os.makedirs(gas_folder, exist_ok=True)

        last_date = get_last_downloaded_date(gas_name)

        if last_date:
            current_date = last_date + timedelta(days=1)
            print(f"  ▶ Продолжаем с {current_date}")
        else:
            current_date = last_available_day
            print("  ▶ Нет файлов — стартуем с последнего дня")

        while current_date <= last_available_day:
            next_date = current_date + timedelta(days=1)
            date_str = current_date.strftime("%Y-%m-%d")
            outfile = os.path.join(gas_folder, f"{gas_name}_{date_str}.tif")

            if os.path.exists(outfile):
                current_date = next_date
                continue

            print(f"  ⬇ {date_str}")

            col = (
                ee.ImageCollection(dataset)
                .select(band)
                .filterDate(str(current_date), str(next_date))
            )

            if col.size().getInfo() == 0:
                print("    ⚠ Нет данных")
                current_date = next_date
                continue

            img = col.mean().clip(region)

            try:
                url = img.getDownloadURL(
                    {"scale": 7000, "region": region, "format": "GEO_TIFF"}
                )
                r = requests.get(url, timeout=120)
                r.raise_for_status()
            except Exception as e:
                print(f"    ❌ Ошибка: {e}")
                return

            with open(outfile, "wb") as f:
                f.write(r.content)

            print("    ✔ Сохранено")
            current_date = next_date


# ---------- AUTO MODE ----------
if __name__ == "__main__":
    print("🟢 Автоскачивание запущено (00:00 UTC)")

    while True:
        sleep_until_midnight_utc()
        try:
            run_sync()
        except Exception as e:
            print(f"🔥 Критическая ошибка: {e}")
