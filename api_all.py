import json
import os
import shutil
from datetime import datetime

import numpy as np
import rioxarray
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from shapely.geometry import shape

# -------------------- LOAD ENV --------------------
load_dotenv()

# -------------------- APP --------------------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.options("/{full_path:path}")
def options_handler(full_path: str):
    return Response(status_code=204)


# -------------------- ENV PATHS --------------------
SAVE_PATH = os.getenv("SAVE_PATH")
GEOJSON_PATH = os.getenv("GEOJSON_PATH")

if not SAVE_PATH:
    raise RuntimeError("SAVE_PATH is not set in environment")

if not GEOJSON_PATH:
    raise RuntimeError("GEOJSON_PATH is not set in environment")

# -------------------- GAS CONFIG --------------------
GAS_UNITS = {
    "CH4": "ppm",
    "CO": "mol/km²",
    "NO2": "mol/km²",
    "SO2": "mol/km²",
    "HCHO": "mol/km²",
    "O3": "mol/m²",
    "AERAI": "unitless",
}


# -------------------- HELPERS --------------------
def get_gas_dir(gas: str) -> str:
    return os.path.join(SAVE_PATH, gas.upper())


def list_tiffs(gas: str):
    folder = get_gas_dir(gas)
    if not os.path.exists(folder):
        return []
    return sorted(
        os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".tif")
    )


def extract_gas_from_filename(filename: str) -> str:
    gas = filename.split("_")[0].upper()
    if gas not in GAS_UNITS:
        raise ValueError(f"Unknown gas '{gas}'")
    return gas


def extract_period_from_filename(fname: str) -> str:
    return fname.replace(".tif", "").split("_")[-1]


def to_dd_mm_yyyy(date_str: str) -> str:
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


# -------------------- ANALYTICS API --------------------
@app.get("/api/air_monitoring_points")
async def air_monitoring_points(gas: str, region: int):
    gas = gas.upper()

    if gas not in GAS_UNITS:
        return JSONResponse({"error": "Unknown gas"}, 400)

    if not os.path.exists(GEOJSON_PATH):
        return JSONResponse({"error": "GeoJSON file not found"}, 404)

    with open(GEOJSON_PATH, "r", encoding="utf-8") as f:
        gj = json.load(f)

    feature = next(
        (
            feat
            for feat in gj["features"]
            if str(feat["properties"].get("region_soato")) == str(region)
        ),
        None,
    )

    if not feature:
        return JSONResponse({"error": "Region not found"}, 404)

    region_polygon = shape(feature["geometry"])

    tiffs = list_tiffs(gas)
    if not tiffs:
        return JSONResponse({"error": "No TIFF files"}, 404)

    results = []

    for tif_path in tiffs:
        fname = os.path.basename(tif_path)
        period = to_dd_mm_yyyy(extract_period_from_filename(fname))

        da = rioxarray.open_rasterio(tif_path).squeeze()
        da = da.rio.write_crs("EPSG:4326", inplace=False)

        clipped = da.rio.clip([region_polygon.__geo_interface__], drop=False)

        mean_value = compute_mean_for_gas(gas, clipped.values.flatten())

        results.append(
            {
                "gas": gas,
                "mean": mean_value,
                "unit": GAS_UNITS[gas],
                "period": period,
            }
        )

    return results


# -------------------- UPLOAD API --------------------
@app.post("/api/upload_tiff")
async def upload_tiff(file: UploadFile = File(...)):
    if not file.filename or not file.filename.endswith(".tif"):
        raise HTTPException(400, "Only .tif files allowed")

    try:
        gas = extract_gas_from_filename(file.filename)
    except ValueError as e:
        raise HTTPException(400, str(e))

    gas_dir = get_gas_dir(gas)
    os.makedirs(gas_dir, exist_ok=True)

    save_path = os.path.join(gas_dir, file.filename)

    with open(save_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    return {"status": "ok", "gas": gas, "saved_to": save_path}
