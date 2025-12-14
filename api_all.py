import json
import os
from datetime import datetime

import numpy as np
import rioxarray
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from shapely.geometry import shape

# ----------------------------------------------------------
# LOAD ENV
# ----------------------------------------------------------

load_dotenv()

# ----------------------------------------------------------
# APP + CORS
# ----------------------------------------------------------

app = FastAPI()

# CORS — важно, чтобы он был ПЕРВЫМ!
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ngrok требует *
    allow_credentials=False,  # иначе нельзя использовать *
    allow_methods=["*"],
    allow_headers=["*"],
)


# Фикс для ngrok FREE — обрабатываем preflight вручную
@app.options("/{full_path:path}")
def options_handler(full_path: str):
    return Response(status_code=204)


# ----------------------------------------------------------
# CONFIG
# ----------------------------------------------------------

TIFF_ROOT = os.getenv("TIFF_ROOT", "data/new_tif")

LEFT_LON = float(os.getenv("LEFT_LON", 48))
RIGHT_LON = float(os.getenv("RIGHT_LON", 80))
BOTTOM_LAT = float(os.getenv("BOTTOM_LAT", 32))
TOP_LAT = float(os.getenv("TOP_LAT", 48))

# GeoJSON path
geojson_env = os.getenv("GEOJSON_PATH")
if geojson_env and geojson_env.strip():
    GEOJSON_PATH = os.path.abspath(geojson_env)
else:
    GEOJSON_PATH = (
        "/home/temur/Documents/Work/goo/geojson/uzbekistan_regions_backup.geojson"
    )

print("Using GEOJSON_PATH:", GEOJSON_PATH)
print("Exists:", os.path.exists(GEOJSON_PATH))

GAS_UNITS = {
    "CH4": "ppm",
    "CO": "mol/km²",
    "NO2": "mol/km²",
    "SO2": "mol/km²",
    "HCHO": "mol/km²",
    "O3": "mol/m²",
    "AERAI": "unitless",
}


# ----------------------------------------------------------
# HELPERS
# ----------------------------------------------------------


def get_gas_dir(gas):
    return os.path.join(TIFF_ROOT, gas.upper())


def list_tiffs(gas):
    folder = get_gas_dir(gas)
    if not os.path.exists(folder):
        return []
    return sorted(
        [os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".tif")]
    )


def extract_period_from_filename(fname):
    # format: CH4_2025-11-26.tif
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


# ----------------------------------------------------------
# API
# ----------------------------------------------------------


@app.get("/api/air_monitoring_points")
async def air_monitoring_points(gas: str, region: int):
    gas = gas.upper()

    if gas not in GAS_UNITS:
        return JSONResponse({"error": f"Unknown gas '{gas}'"}, status_code=400)

    if not os.path.exists(GEOJSON_PATH):
        return JSONResponse({"error": "GeoJSON file not found"}, status_code=404)

    # Load GeoJSON
    with open(GEOJSON_PATH, "r", encoding="utf-8") as f:
        gj = json.load(f)

    # Find region by SOATO
    feature = next(
        (
            feat
            for feat in gj["features"]
            if str(feat["properties"].get("region_soato")) == str(region)
        ),
        None,
    )

    if not feature:
        return JSONResponse({"error": f"Region {region} not found"}, 404)

    region_polygon = shape(feature["geometry"])

    # List TIFF files
    tiffs = list_tiffs(gas)
    if not tiffs:
        return JSONResponse({"error": f"No TIFF files for gas {gas}"}, 404)

    results = []

    for tif_path in tiffs:
        fname = os.path.basename(tif_path)
        iso_date = extract_period_from_filename(fname)
        period = to_dd_mm_yyyy(iso_date)

        # Read raster
        da = rioxarray.open_rasterio(tif_path).squeeze()
        da = da.rio.write_crs("EPSG:4326", inplace=False)

        # Clip by region
        clipped = da.rio.clip([region_polygon.__geo_interface__], drop=False)

        # Compute mean value
        values = clipped.values.flatten()
        mean_value = compute_mean_for_gas(gas, values)

        results.append(
            {
                "gas": gas,
                "mean": mean_value,
                "unit": GAS_UNITS[gas],
                "period": period,
            }
        )

    return results
