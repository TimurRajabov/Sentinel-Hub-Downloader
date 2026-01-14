# main.py
# -*- coding: utf-8 -*-
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from typing import List, Optional, Tuple, Union

import numpy as np
import rioxarray
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from shapely.geometry import shape
from shapely.ops import transform as shp_transform
from starlette.background import BackgroundTask

from make_word import build_docx  # твоя функция генерации DOCX

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
#    ENV / PATHS
# =======================
SAVE_PATH = os.getenv("SAVE_PATH")  # Sentinel-5P root (например /data)
OUTPUT_ROOT = os.getenv("OUTPUT_ROOT")  # ADS root (например /output)
GEOJSON_PATH = os.getenv("GEOJSON_PATH")  # регионы по soato
GEOJSON_PATH_1 = os.getenv("GEOJSON_PATH_1")  # Узбекистан полигон (legenda)

if not SAVE_PATH:
    raise RuntimeError("SAVE_PATH is not set in environment")
if not GEOJSON_PATH:
    raise RuntimeError("GEOJSON_PATH is not set in environment")
if not GEOJSON_PATH_1:
    raise RuntimeError("GEOJSON_PATH_1 is not set in environment")


MINTAQA_SHP = os.getenv("MINTAQA_SHP")
TUMAN_SHP = os.getenv("TUMAN_SHP")
TEXT_JSON_PATH = os.getenv("TEXT_JSON_PATH")

ALLOWED_GASES = {"AERAI", "CH4", "CO", "HCHO", "NO2", "O3", "SO2"}
ALLOWED_DAYS = {7, 15, 30}

SENTINEL_ONLY_GASES = {"CH4", "AERAI"}

GAS_UNITS = {
    "CH4": "ppm",
    "CO": "mol/m²",
    "NO2": "mol/m²",
    "SO2": "mol/m²",
    "HCHO": "mol/m²",
    "O3": "mol/m²",
    "AERAI": "unitless",
}


ADS_SCALE = {
    "CO": 35.7015,
    "NO2": 21.73,
    "SO2": 15.6,
    "HCHO": 33.3,
    "O3": 20.83,
}


def get_gas_dir(gas: str, root: str) -> str:
    return os.path.join(root, gas.upper())


def list_tiffs(gas: str, root: str) -> List[str]:
    folder = get_gas_dir(gas, root)
    if not os.path.isdir(folder):
        return []
    return sorted(
        os.path.join(folder, f)
        for f in os.listdir(folder)
        if f.lower().endswith(".tif")
    )


def extract_period_from_filename(fname: str) -> str:
    base = os.path.basename(fname).replace(".tif", "")
    return base.split("_")[-1]


def extract_date_from_filename_any(fname: str) -> Optional[str]:

    base = os.path.basename(fname)
    m = re.search(r"\d{4}-\d{2}-\d{2}", base)
    return m.group(0) if m else None


def to_dd_mm_yyyy(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%m-%Y")


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
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "invalid date")


def _reproject_polygon_to_raster_crs(region_polygon, raster_crs):
    if raster_crs is None:
        return region_polygon

    try:
        raster_crs_str = raster_crs.to_string()
    except Exception:
        raster_crs_str = str(raster_crs)

    if str(raster_crs_str).lower() == "epsg:4326":
        return region_polygon

    from pyproj import Transformer

    transformer = Transformer.from_crs("EPSG:4326", raster_crs_str, always_xy=True)
    return shp_transform(lambda x, y: transformer.transform(x, y), region_polygon)


def clip_and_get_values(tif_path: str, region_polygon) -> np.ndarray:
    da = rioxarray.open_rasterio(tif_path, masked=True).squeeze()

    if da.rio.crs is None:
        da = da.rio.write_crs("EPSG:4326", inplace=False)

    poly_in_raster_crs = _reproject_polygon_to_raster_crs(region_polygon, da.rio.crs)

    clipped = da.rio.clip(
        [poly_in_raster_crs.__geo_interface__], drop=True, all_touched=False
    )
    arr = clipped.values

    if np.ma.isMaskedArray(arr):
        valid = (~arr.mask) & np.isfinite(arr.data)
        vals = arr.data[valid]
    else:
        vals = arr[np.isfinite(arr)]

    return np.asarray(vals, dtype="float64").ravel()


def compute_mean_raw(
    values: np.ndarray, *, round_ndigits: Optional[int] = None
) -> Optional[float]:
    if values.size == 0:
        return None

    values = values[np.isfinite(values)]
    if values.size == 0:
        return None

    values = values[(values >= 0) & (values < 1e30)]
    if values.size == 0:
        return None

    m = float(np.nanmean(values))
    if round_ndigits is None:
        return m
    return round(m, round_ndigits)


def compute_stats_raw(values: np.ndarray) -> dict:
    if values.size == 0:
        return {"min": None, "max": None, "mean": None}

    v = values[np.isfinite(values)]
    v = v[(v >= 0) & (v < 1e30)]
    if v.size == 0:
        return {"min": None, "max": None, "mean": None}

    return {
        "min": float(np.nanmin(v)),
        "max": float(np.nanmax(v)),
        "mean": float(np.nanmean(v)),
    }


def apply_scale_stats(stats: dict, gas: str, effective_source: str) -> dict:
    if effective_source != "ADS":
        return stats

    k = ADS_SCALE.get(gas)
    if not k:
        return stats

    out = dict(stats)
    for key in ("min", "max", "mean"):
        if out.get(key) is not None:
            out[key] = out[key] * k
    return out


def resolve_effective_source_and_root(
    gas: str, requested_source: str
) -> Tuple[str, str]:
    gas = gas.upper().strip()
    requested_source = requested_source.upper().strip()

    if gas in SENTINEL_ONLY_GASES:
        return "S5", SAVE_PATH

    if requested_source == "S5":
        return "S5", SAVE_PATH

    if not OUTPUT_ROOT:
        raise HTTPException(
            500, "OUTPUT_ROOT is not set in environment (required for ADS)"
        )
    return "ADS", OUTPUT_ROOT


def _extract_date_from_tif_path(tif_path: str) -> Optional[datetime]:
    date_str = extract_date_from_filename_any(tif_path)
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        return None


def find_latest_tif(gas: str, root: str) -> Optional[str]:
    tiffs = list_tiffs(gas, root)
    if not tiffs:
        return None

    best_path = None
    best_date = None

    for p in tiffs:
        d = _extract_date_from_tif_path(p)
        if d is None:
            continue
        if best_date is None or d > best_date:
            best_date = d
            best_path = p

    return best_path


def load_polygon_from_geojson(path: str):
    if not os.path.isfile(path):
        raise HTTPException(404, f"GeoJSON file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        gj = json.load(f)

    if isinstance(gj, dict) and gj.get("type") == "FeatureCollection":
        feats = gj.get("features") or []
        if not feats:
            raise HTTPException(400, f"{path} has empty features")
        geom = feats[0].get("geometry")
        if not geom:
            raise HTTPException(400, f"{path} first feature has no geometry")
        return shape(geom)

    if isinstance(gj, dict) and gj.get("type") == "Feature":
        geom = gj.get("geometry")
        if not geom:
            raise HTTPException(400, f"{path} feature has no geometry")
        return shape(geom)

    return shape(gj)


class DownloadRequest(BaseModel):
    gas: Union[str, List[str]] = Field(..., description="Газ или список газов")
    date: str = Field(..., description="YYYY-MM-DD")
    region: int = Field(..., description="parent_cod (int)")
    count_date: int = Field(30, description="7 / 15 / 30 (сколько дней для графика)")


@app.get("/api/air_monitoring_points")
async def air_monitoring_points(
    gas: str,
    region: int,
    source: str = Query("S5", description="Источник данных: S5 или ADS"),
):
    gas = gas.upper().strip()
    source = source.upper().strip()

    if gas not in GAS_UNITS:
        return JSONResponse(
            {"error": f"Unknown gas. Allowed: {sorted(GAS_UNITS.keys())}"}, 400
        )

    if source not in {"S5", "ADS"}:
        return JSONResponse({"error": "source must be S5 or ADS"}, 400)

    if not os.path.isfile(GEOJSON_PATH):
        return JSONResponse({"error": f"GeoJSON file not found: {GEOJSON_PATH}"}, 404)

    with open(GEOJSON_PATH, "r", encoding="utf-8") as f:
        gj = json.load(f)

    feature = next(
        (
            feat
            for feat in gj.get("features", [])
            if str(feat.get("properties", {}).get("region_soato")) == str(region)
        ),
        None,
    )

    if not feature:
        return JSONResponse({"error": "Region not found"}, 404)

    region_polygon = shape(feature["geometry"])

    try:
        effective_source, root = resolve_effective_source_and_root(gas, source)
    except HTTPException as e:
        return JSONResponse({"error": e.detail}, e.status_code)

    tiffs = list_tiffs(gas, root)
    if not tiffs:
        return JSONResponse(
            {
                "error": f"No TIFF files for gas {gas} in {get_gas_dir(gas, root)}",
                "source": effective_source,
            },
            404,
        )

    results = []

    for tif_path in tiffs:
        # ПРАВКА: достаём дату из имени нормально (ADS тоже)
        period_raw = extract_date_from_filename_any(
            tif_path
        ) or extract_period_from_filename(os.path.basename(tif_path))

        try:
            period = (
                to_dd_mm_yyyy(period_raw)
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(period_raw))
                else str(period_raw)
            )
        except Exception:
            period = str(period_raw)

        try:
            vals = clip_and_get_values(tif_path, region_polygon)
            mean_raw = compute_mean_raw(vals, round_ndigits=None)

            # масштаб для ADS (только mean)
            if effective_source == "ADS":
                k = ADS_SCALE.get(gas)
                mean_value = (
                    (mean_raw * k)
                    if (mean_raw is not None and k is not None)
                    else mean_raw
                )
            else:
                mean_value = mean_raw

            pixels = int(vals.size)
        except Exception as e:
            results.append(
                {
                    "gas": gas,
                    "source": effective_source,
                    "requested_source": source,
                    "mean": None,
                    "unit": GAS_UNITS[gas],
                    "period": period,
                    "pixels": 0,
                    "error": str(e),
                }
            )
            continue

        results.append(
            {
                "gas": gas,
                "source": effective_source,
                "mean": mean_value,
                "unit": GAS_UNITS[gas],
                "period": period,
            }
        )

    return results


@app.get("/api/legenda")
def legenda(
    gas: str = Query(...),
    source: str = Query("S5", description="S5 или ADS"),
):
    gas = gas.upper().strip()
    source = source.upper().strip()

    if gas not in ALLOWED_GASES:
        raise HTTPException(
            400, f"Unknown gas. Allowed: {', '.join(sorted(ALLOWED_GASES))}"
        )

    if source not in {"S5", "ADS"}:
        raise HTTPException(400, "source must be S5 or ADS")

    uzb_polygon = load_polygon_from_geojson(GEOJSON_PATH_1)

    effective_source, root = resolve_effective_source_and_root(gas, source)

    tif_path = find_latest_tif(gas, root)
    if not tif_path:
        raise HTTPException(
            404,
            f"No valid dated TIFF files for gas={gas} in {get_gas_dir(gas, root)} (source={effective_source})",
        )

    try:
        vals = clip_and_get_values(tif_path, uzb_polygon)
        stats_raw = compute_stats_raw(vals)
        stats = apply_scale_stats(stats_raw, gas, effective_source)
    except Exception as e:
        raise HTTPException(500, f"Failed to compute legenda stats: {e}")

    return {
        "min": stats["min"],
        "max": stats["max"],
        "mean": stats["mean"],
        "unit": GAS_UNITS[gas],
    }


def _last_date_for_gas(gas: str, root: str) -> Optional[datetime]:
    tiffs = list_tiffs(gas, root)
    if not tiffs:
        return None

    dates = []
    for p in tiffs:
        d = _extract_date_from_tif_path(p)
        if d:
            dates.append(d)

    if not dates:
        return None

    return max(dates)


@app.get("/api/lastdate")
def lastdate(gas: str = Query(...)):
    gases = _normalize_gases(gas)
    if not gases:
        raise HTTPException(400, "gas is empty")

    bad = [g for g in gases if g not in ALLOWED_GASES]
    if bad:
        raise HTTPException(
            400,
            f"Unknown gas: {', '.join(bad)}. Allowed: {', '.join(sorted(ALLOWED_GASES))}",
        )

    last_dates = []
    for g in gases:
        last = _last_date_for_gas(g, SAVE_PATH)
        if last is None:
            raise HTTPException(404, f"No valid dated TIFF files for gas: {g}")
        last_dates.append(last)

    common_last = min(last_dates)
    return {"lastdate": common_last.strftime("%Y-%m-%d")}


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
    out_docx = os.path.join(
        tmpdir, f"report_{req.region}_{req.date}_{req.count_date}d.docx"
    )

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
