import os
import shutil
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import cdsapi
import rioxarray
import xarray as xr
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).with_name(".env"))


def env_str(key: str, default: str | None = None) -> str:
    v = os.getenv(key)
    if v is None or not v.strip():
        if default is None:
            raise RuntimeError(f"ENV '{key}' is required")
        return default
    return v.strip()


def env_float(key: str, default: float | None = None) -> float:
    v = os.getenv(key)
    if v is None or not v.strip():
        if default is None:
            raise RuntimeError(f"ENV '{key}' is required")
        return float(default)
    return float(v.strip())


def env_date(key: str, default: str | None = None) -> date:
    v = os.getenv(key)
    if v is None or not v.strip():
        if default is None:
            raise RuntimeError(f"ENV '{key}' is required")
        v = default
    return datetime.strptime(v.strip(), "%Y-%m-%d").date()


OUTPUT_ROOT = env_str("OUTPUT_ROOT", "/output")

LEFT_LON = env_float("LEFT_LON")
RIGHT_LON = env_float("RIGHT_LON")
BOTTOM_LAT = env_float("BOTTOM_LAT")
TOP_LAT = env_float("TOP_LAT")

DATASET_ID = env_str("CAMS_DATASET_ID", "cams-global-atmospheric-composition-forecasts")
REQUEST_TYPE = env_str("CAMS_REQUEST_TYPE", "analysis")
TIMES = [
    t.strip()
    for t in env_str("CAMS_TIMES", "00:00,06:00,12:00,18:00").split(",")
    if t.strip()
]
FORMAT = env_str("CAMS_FORMAT", "netcdf_zip")
LEADTIME_HOUR = env_str("CAMS_LEADTIME_HOUR", "0")

START_DAY_1 = env_date("CAMS_TART_DAY")
END_DAY_1 = env_date("CAMS_ND_DAY")

# retry settings (можешь вынести в env)
RETRY_MAX = int(env_str("CAMS_RETRY_MAX", "8"))
SLEEP_BASE = int(env_str("CAMS_SLEEP_BASE", "10"))  # seconds


GASES = {
    "CO": ("total_column_carbon_monoxide", "tcco"),
    "NO2": ("total_column_nitrogen_dioxide", "tcno2"),
    "SO2": ("total_column_sulphur_dioxide", "tcso2"),
    "HCHO": ("total_column_formaldehyde", "tchcho"),
    "O3": ("total_column_ozone", "gtco3"),
}


def month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def month_end(d: date) -> date:
    if d.month == 12:
        return date(d.year, 12, 31)
    return date(d.year, d.month + 1, 1) - timedelta(days=1)


def daterange(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def safe_remove(path: str):
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(f"⚠ cleanup failed {path}: {e}")


def extract_first_nc(zip_path: str, extract_dir: str) -> str:
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(extract_dir)
        nc_files = [
            os.path.join(extract_dir, n)
            for n in z.namelist()
            if n.lower().endswith((".nc", ".nc4", ".cdf"))
        ]
    if not nc_files:
        raise RuntimeError("В ZIP не найден NetCDF (.nc/.nc4/.cdf).")
    return nc_files[0]


def pick_dataarray(ds: xr.Dataset, ads_var: str, short_var: str) -> xr.DataArray:
    if short_var in ds.data_vars:
        return ds[short_var]
    if ads_var in ds.data_vars:
        return ds[ads_var]
    if len(ds.data_vars) == 1:
        only = list(ds.data_vars.keys())[0]
        print(f"⚠ Не нашёл {short_var}/{ads_var}. Беру единственную переменную: {only}")
        return ds[only]
    raise RuntimeError(f"Не нашёл переменную. Есть: {list(ds.data_vars.keys())}")


def collapse_to_2d_latlon(da: xr.DataArray) -> xr.DataArray:
    if "longitude" in da.dims and "latitude" in da.dims:
        x_dim, y_dim = "longitude", "latitude"
    elif "lon" in da.dims and "lat" in da.dims:
        x_dim, y_dim = "lon", "lat"
    else:
        raise RuntimeError(f"Не нашёл lon/lat. dims={da.dims}")

    for dim in list(da.dims):
        if dim in (x_dim, y_dim):
            continue
        if da.sizes.get(dim, 1) > 1:
            da = da.mean(dim)
        else:
            da = da.isel({dim: 0})

    da = da.squeeze()

    if da[y_dim].values[0] < da[y_dim].values[-1]:
        da = da.sortby(y_dim, ascending=False)

    da = da.rio.set_spatial_dims(x_dim=x_dim, y_dim=y_dim, inplace=False)
    da = da.rio.write_crs("EPSG:4326")
    return da


def retrieve_with_retry(
    client: cdsapi.Client, dataset: str, request: dict, target: str
):
    for attempt in range(1, RETRY_MAX + 1):
        try:
            client.retrieve(dataset, request, target)
            return
        except Exception as e:
            wait = min(600, SLEEP_BASE * (2 ** (attempt - 1)))  # up to 10 min
            print(f"❌ retrieve failed (attempt {attempt}/{RETRY_MAX}): {e}")
            print(f"⏳ sleep {wait}s then retry...")
            time.sleep(wait)
    raise RuntimeError("Max retries exceeded")


def download_month(
    client: cdsapi.Client,
    gas_label: str,
    ads_var: str,
    short_var: str,
    m_start: date,
    m_end: date,
):
    # директория газа
    out_dir = os.path.join(OUTPUT_ROOT, gas_label)
    os.makedirs(out_dir, exist_ok=True)

    # marker: если месяц уже обработан
    marker = os.path.join(out_dir, f"_DONE_{m_start.strftime('%Y-%m')}.txt")
    if os.path.exists(marker):
        print(f"✔ [{gas_label}] month already done: {m_start.strftime('%Y-%m')}")
        return

    dates = [d.strftime("%Y-%m-%d") for d in daterange(m_start, m_end)]
    area = [TOP_LAT, LEFT_LON, BOTTOM_LAT, RIGHT_LON]

    tmp_zip = os.path.join(out_dir, f"_tmp_{gas_label}_{m_start.strftime('%Y-%m')}.zip")
    tmp_dir = os.path.join(out_dir, f"_tmp_{gas_label}_{m_start.strftime('%Y-%m')}")

    request = {
        "date": dates,
        "type": REQUEST_TYPE,
        "variable": ads_var,
        "time": TIMES,
        "leadtime_hour": LEADTIME_HOUR,
        "format": FORMAT,
        "area": area,
    }

    print(
        f"\n=== {gas_label} | month {m_start.strftime('%Y-%m')} ({len(dates)} days) ==="
    )
    retrieve_with_retry(client, DATASET_ID, request, tmp_zip)

    os.makedirs(tmp_dir, exist_ok=True)
    try:
        nc_path = extract_first_nc(tmp_zip, tmp_dir)
        with xr.open_dataset(nc_path, engine="netcdf4") as ds:
            da = pick_dataarray(ds, ads_var=ads_var, short_var=short_var)

            # если есть time, сгруппируем по дням и сохраним GeoTIFF на каждый день
            if "time" in da.dims:
                # time -> date
                day_index = da["time"].dt.floor("D")
                da = da.assign_coords(day=("time", day_index.data))
                daily = da.groupby("day").mean("time")
            else:
                # нет time — считаем что уже дневные данные
                daily = da

            for d in daterange(m_start, m_end):
                day_str = d.strftime("%Y-%m-%d")
                out_tif = os.path.join(out_dir, f"{gas_label}_{day_str}_ADS.tif")
                if os.path.exists(out_tif):
                    continue

                if "day" in daily.dims:
                    sel = daily.sel(day=numpy_datetime64(day_str))
                else:
                    # fallback: одно поле на весь месяц/файл
                    sel = daily

                sel = sel.load()
                da2d = collapse_to_2d_latlon(sel)
                da2d.rio.to_raster(out_tif)
                print(f"✅ [{gas_label}] saved {out_tif}")

        with open(marker, "w", encoding="utf-8") as f:
            f.write(f"done {datetime.now().isoformat()}\n")
    finally:
        safe_remove(tmp_zip)
        safe_remove(tmp_dir)


def numpy_datetime64(day_str: str):
    import numpy as np

    return np.datetime64(day_str)


def main():
    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    print("OUTPUT_ROOT:", OUTPUT_ROOT)
    print("DATE RANGE:", START_DAY_1, "->", END_DAY_1)
    print("BBOX:", TOP_LAT, LEFT_LON, BOTTOM_LAT, RIGHT_LON)
    print("DATASET:", DATASET_ID, "TYPE:", REQUEST_TYPE, "TIMES:", TIMES)

    client = cdsapi.Client(timeout=600, retry_max=3)

    cur = month_start(START_DAY_1)
    last = month_start(END_DAY_1)

    while cur <= last:
        m_s = cur
        m_e = month_end(cur)

        if m_s < START_DAY_1:
            m_s = START_DAY_1
        if m_e > END_DAY_1:
            m_e = END_DAY_1

        for label, (ads_var, short_var) in GASES.items():
            download_month(client, label, ads_var, short_var, m_s, m_e)

        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)


if __name__ == "__main__":
    main()
