# main.py
# -*- coding: utf-8 -*-
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from typing import List, Union

import numpy as np
import rioxarray
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from shapely.geometry import shape
from starlette.background import BackgroundTask

from make_word import build_docx  # <-- твоя функция генерации DOCX

load_dotenv()

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


# =======================
#    ТВОИ ENV / PATHS
# =======================
SAVE_PATH = os.getenv("SAVE_PATH")
GEOJSON_PATH = os.getenv("GEOJSON_PATH")

if not SAVE_PATH:
    raise RuntimeError("SAVE_PATH is not set in environment")
if not GEOJSON_PATH:
    raise RuntimeError("GEOJSON_PATH is not set in environment")


# =======================
#    DOCX ENV / PATHS
# =======================
# Можно положить в .env (лучше так):
RASTERS_ROOT = os.getenv("RASTERS_ROOT")        # напр: /data/rasters или C:\\...
MINTAQA_SHP = os.getenv("MINTAQA_SHP")          # путь к Mintaqa.shp
TUMAN_SHP = os.getenv("TUMAN_SHP")              # путь к Tuman.shp
TEXT_JSON_PATH = os.getenv("TEXT_JSON_PATH")    # путь к text.json

# Разрешённые газы/дни для отчёта
ALLOWED_GASES = {"AERAI", "CH4", "CO", "HCHO", "NO2", "O3", "SO2"}
ALLOWED_DAYS = {7, 15, 30}


GAS_UNITS = {
    "CH4": "ppm",
    "CO": "mol/km²",
    "NO2": "mol/km²",
    "SO2": "mol/km²",
    "HCHO": "mol/km²",
    "O3": "mol/m²",
    "AERAI": "unitless",
}


# =======================
#      HELPERS (TIFF)
# =======================
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


# =======================
#   HELPERS (DOCX)
# =======================
class DownloadRequest(BaseModel):
    gas: Union[str, List[str]] = Field(..., description="Газ или список газов. Можно строкой: 'NO2,CO'")
    date: str = Field(..., description="YYYY-MM-DD")
    region: int = Field(..., description="parent_cod (int)")
    count_date: int = Field(30, description="7 / 15 / 30 (сколько дней для графика)")


def _cleanup_dir(path: str):
    shutil.rmtree(path, ignore_errors=True)


def _normalize_gases(gas: Union[str, List[str]]) -> List[str]:
    if isinstance(gas, str):
        parts = [g.strip() for g in gas.split(",") if g.strip()]
    else:
        parts = [str(g).strip() for g in gas if str(g).strip()]

    gases = [g.upper() for g in parts]

    seen = set()
    out = []
    for g in gases:
        if g not in seen:
            out.append(g)
            seen.add(g)
    return out


def _validate_date(date_str: str):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        raise HTTPException(400, "date must be YYYY-MM-DD")


# =======================
#       ENDPOINTS
# =======================
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


@app.post("/api/download")
def download_docx(req: DownloadRequest):
    gases = _normalize_gases(req.gas)
    if not gases:
        raise HTTPException(400, "gas is empty")

    bad = [g for g in gases if g not in ALLOWED_GASES]
    if bad:
        raise HTTPException(
            400,
            f"Unknown gas: {', '.join(bad)}. Allowed: {', '.join(sorted(ALLOWED_GASES))}",
        )

    _validate_date(req.date)

    if req.count_date not in ALLOWED_DAYS:
        raise HTTPException(400, f"count_date must be one of {sorted(ALLOWED_DAYS)}")

    # Проверка ENV путей для DOCX
    if not SAVE_PATH:
        raise HTTPException(500, "SAVE_PATH is not set in environment")
    if not MINTAQA_SHP:
        raise HTTPException(500, "MINTAQA_SHP is not set in environment")
    if not TUMAN_SHP:
        raise HTTPException(500, "TUMAN_SHP is not set in environment")
    if not TEXT_JSON_PATH:
        raise HTTPException(500, "TEXT_JSON_PATH is not set in environment")

    if not os.path.isdir(SAVE_PATH):
        raise HTTPException(500, f"SAVE_PATH not found: {SAVE_PATH}")
    if not os.path.isfile(TEXT_JSON_PATH):
        raise HTTPException(500, f"text.json not found: {TEXT_JSON_PATH}")
    if not os.path.isfile(MINTAQA_SHP):
        raise HTTPException(500, f"MINTAQA_SHP not found: {MINTAQA_SHP}")
    if not os.path.isfile(TUMAN_SHP):
        raise HTTPException(500, f"TUMAN_SHP not found: {TUMAN_SHP}")

    tmpdir = tempfile.mkdtemp(prefix="airrep_api_")
    out_docx = os.path.join(tmpdir, f"report_{req.region}_{req.date}_{req.count_date}d.docx")

    try:
        build_docx(
            gases=gases,
            date_str=req.date,
            parent_cod=int(req.region),
            count_date=int(req.count_date),
            rasters_root=SAVE_PATH,
            mintaqa_shp=MINTAQA_SHP,
            tuman_shp=TUMAN_SHP,
            text_json_path=TEXT_JSON_PATH,
            out_docx=out_docx,
        )
    except FileNotFoundError as e:
        _cleanup_dir(tmpdir)
        raise HTTPException(404, f"File not found: {e}")
    except Exception as e:
        _cleanup_dir(tmpdir)
        raise HTTPException(500, f"Failed to build report: {e}")

    if not os.path.isfile(out_docx):
        _cleanup_dir(tmpdir)
        raise HTTPException(500, "DOCX was not created")

    filename = f"air_report_{req.region}_{req.date}_{req.count_date}d.docx"
    return FileResponse(
        path=out_docx,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
        background=BackgroundTask(_cleanup_dir, tmpdir),
    )


