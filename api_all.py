import os
import json
import rioxarray
import numpy as np
from shapely.geometry import shape
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from datetime import datetime


# ----------------------------------------------------------
# CONFIG
# ----------------------------------------------------------

TIFF_ROOT = os.getenv("TIFF_ROOT", "data/new_tif")

# BBOX — оставляем, они нужны только для скачивания, не для анализа
LEFT_LON = float(os.getenv("LEFT_LON", 48))
RIGHT_LON = float(os.getenv("RIGHT_LON", 80))
BOTTOM_LAT = float(os.getenv("BOTTOM_LAT", 32))
TOP_LAT = float(os.getenv("TOP_LAT", 48))

# GeoJSON для регионов Узбекистана
GEOJSON_PATH = os.getenv(
    "GEOJSON_PATH",
    "/home/temur/Documents/Work/goo/geojson/uzbekistan_regions_backup.geojson"
)

GAS_UNITS = {
    "CH4": "ppm",
    "CO": "mol/km²",
    "NO2": "mol/km²",
    "SO2": "mol/km²",
    "HCHO": "mol/km²",
    "O3": "mol/m²",
    "AERAI": "unitless"
}

app = FastAPI()


# ----------------------------------------------------------
# HELPERS
# ----------------------------------------------------------

def get_gas_dir(gas):
    return os.path.join(TIFF_ROOT, gas.upper())


def list_tiffs(gas):
    folder = get_gas_dir(gas)
    if not os.path.exists(folder):
        return []
    return sorted([
        os.path.join(folder, f)
        for f in os.listdir(folder)
        if f.endswith(".tif")
    ])


def extract_period_from_filename(fname):
    return fname.replace(".tif", "").split("_")[-1]


def to_dd_mm_yyyy(date_str):
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%m-%Y")


def compute_mean_for_gas(gas: str, values: np.ndarray) -> float:
    finite = values[np.isfinite(values)]
    finite = finite[(finite >= 0) & (finite < 1e20)]

    if finite.size == 0:
        return 0.0

    raw_mean = float(np.nanmean(finite))

    if gas == "CH4":
        return round(raw_mean / 1000.0, 3)

    if gas in ("NO2", "SO2", "HCHO"):
        return round(raw_mean * 1e6, 3)

    return round(raw_mean, 3)


@app.get("/api/air_monitoring_points")
async def air_monitoring_points(gas: str, region: int):
    gas = gas.upper()

    # Проверяем газ
    if gas not in GAS_UNITS:
        return JSONResponse({"error": f"Unknown gas '{gas}'"}, status_code=400)

    # Проверяем GeoJSON
    if not os.path.exists(GEOJSON_PATH):
        return JSONResponse({"error": "GeoJSON file not found"}, status_code=404)

    # Читаем GeoJSON
    with open(GEOJSON_PATH, "r", encoding="utf-8") as f:
        gj = json.load(f)

    # Ищем регион
    feature = next(
        (
            feat for feat in gj["features"]
            if str(feat["properties"].get("region_soato")) == str(region)
        ),
        None
    )

    if not feature:
        return JSONResponse({"error": f"Region {region} not found"}, status_code=404)

    region_polygon = shape(feature["geometry"])

    # TIFF-файлы газа
    tiffs = list_tiffs(gas)
    if not tiffs:
        return JSONResponse({"error": f"No TIFF files for gas {gas}"}, 404)

    results = []

    for tif_path in tiffs:
        fname = os.path.basename(tif_path)

        iso_date = extract_period_from_filename(fname)
        period = to_dd_mm_yyyy(iso_date)

        da = rioxarray.open_rasterio(tif_path).squeeze()
        da = da.rio.write_crs("EPSG:4326", inplace=False)

        clipped = da.rio.clip([region_polygon.__geo_interface__], drop=False)

        values = clipped.values.flatten()
        mean_value = compute_mean_for_gas(gas, values)

        results.append(
            {
                "gas": gas,
                "mean": mean_value,
                "unit": GAS_UNITS[gas],
                "period": period
            }
        )

    return results
