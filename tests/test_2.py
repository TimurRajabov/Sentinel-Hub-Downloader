import os
import random
import time
from datetime import date, datetime, timedelta

import ee  # type: ignore
import requests
from dotenv import load_dotenv
from requests.exceptions import RequestException, Timeout

# ---------- LOAD ENV ----------
load_dotenv()

PROJECT_ID = os.getenv("PROJECT_ID")

LEFT_LON = float(os.getenv("LEFT_LON"))
RIGHT_LON = float(os.getenv("RIGHT_LON"))
BOTTOM_LAT = float(os.getenv("BOTTOM_LAT"))
TOP_LAT = float(os.getenv("TOP_LAT"))

SAVE_PATH = os.getenv("SAVE_PATH", "/data")

START_DATE = os.getenv("START_DATE")  # YYYY-MM-DD
END_DATE = os.getenv("END_DATE")  # YYYY-MM-DD

# Ретраи для EE/HTTP
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "6"))
BASE_SLEEP = float(os.getenv("BASE_SLEEP", "2.0"))
MAX_SLEEP = float(os.getenv("MAX_SLEEP", "120.0"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "300"))

# Поведение при провале дня после всех ретраев:
# 1 = пропускать день и идти дальше
# 0 = остановить текущий газ на этом дне (повторим позже)
SKIP_DAY_ON_FAIL = os.getenv("SKIP_DAY_ON_FAIL", "1") == "1"

# Вечный режим
COOLDOWN_HOURS_ON_FAILURE = float(os.getenv("COOLDOWN_HOURS_ON_FAILURE", "5"))
SLEEP_BETWEEN_PASSES_SEC = int(os.getenv("SLEEP_BETWEEN_PASSES_SEC", "300"))


# ---------- VALIDATE DATES ----------
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
def _backoff_sleep(attempt: int) -> None:
    sleep_s = min(MAX_SLEEP, BASE_SLEEP * (2 ** (attempt - 1)))
    sleep_s += random.random()
    print(f"    ⏳ backoff {sleep_s:.1f} сек...")
    time.sleep(sleep_s)


def get_last_downloaded_date(gas_name: str) -> date | None:
    folder = os.path.join(SAVE_PATH, gas_name)
    if not os.path.exists(folder):
        return None

    dates: list[date] = []
    for f in os.listdir(folder):
        try:
            # ожидаем: GAS_YYYY-MM-DD.tif
            d = f.split("_", 1)[1].replace(".tif", "")
            dates.append(datetime.strptime(d, "%Y-%m-%d").date())
        except Exception:
            pass

    return max(dates) if dates else None


def download_geotiff_with_retries(img: ee.Image, outfile: str) -> None:
    params = {"scale": 7000, "region": region, "format": "GEO_TIFF"}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            url = img.getDownloadURL(params)

            with requests.get(url, stream=True, timeout=HTTP_TIMEOUT) as r:
                if r.status_code in (429, 500, 502, 503, 504):
                    raise RequestException(f"{r.status_code} {r.reason} for url: {url}")
                r.raise_for_status()

                tmp = outfile + ".part"
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                os.replace(tmp, outfile)
            return

        except (RequestException, Timeout) as e:
            print(f"    ❌ сеть/сервис (попытка {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                _backoff_sleep(attempt)
                continue
            raise

        except Exception as e:
            print(f"    ❌ ошибка (попытка {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                _backoff_sleep(attempt)
                continue
            raise


# ---------- MAIN LOGIC ----------
def run_sync() -> bool:
    """Возвращает True, если были провалы (после всех ретраев), иначе False."""
    had_failures = False

    print(f"\n🚀 Синхронизация газов: {START_DAY} .. {END_DAY} | SAVE_PATH={SAVE_PATH}")

    for gas_name, (dataset, band) in gases.items():
        print(f"\n🧪 Газ: {gas_name}")

        gas_folder = os.path.join(SAVE_PATH, gas_name)
        os.makedirs(gas_folder, exist_ok=True)

        last_date = get_last_downloaded_date(gas_name)

        if last_date:
            current_date = max(last_date + timedelta(days=1), START_DAY)
            print(f"  ▶ Продолжаем с {current_date} (последний файл: {last_date})")
        else:
            current_date = START_DAY
            print(f"  ▶ Нет файлов — стартуем с {START_DAY}")

        if current_date > END_DAY:
            print("  ✅ Уже всё скачано в заданном диапазоне")
            continue

        while current_date <= END_DAY:
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

            # это НЕ ошибка
            if col.size().getInfo() == 0:
                print("    ⚠ Нет данных")
                current_date = next_date
                continue

            img = col.mean().clip(region)

            try:
                download_geotiff_with_retries(img, outfile)
                print("    ✔ Сохранено")
                current_date = next_date

            except Exception as e:
                had_failures = True
                print(f"    ❌ Не удалось скачать {gas_name} за {date_str}: {e}")

                if SKIP_DAY_ON_FAIL:
                    print("    ⏭ Пропускаем день и идём дальше")
                    current_date = next_date
                    continue
                else:
                    print(
                        "    🛑 Останавливаем этот газ на текущей дате (повторим позже)"
                    )
                    break

    return had_failures


if __name__ == "__main__":
    print(f"🟢 Вечный режим газов: диапазон={START_DAY}..{END_DAY}")

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
