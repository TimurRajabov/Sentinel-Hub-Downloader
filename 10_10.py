#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import random
import time
import math
import argparse
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Any, Optional

import io
import zipfile

import ee  # type: ignore
import requests
from dotenv import load_dotenv
from requests.exceptions import RequestException, Timeout

import numpy as np
import rasterio
from rasterio.merge import merge
from PIL import Image

load_dotenv()

# ---------------- env ----------------

PROJECT_ID = os.getenv("PROJECT_ID")
SAVE_PATH = os.getenv("SAVE_PATH", "/data")

# Cloudy_pixel_percentage — фильтр на весь тайл, не на пиксели.
# Пиксели облаков режем через SCL + QA60 ниже.
MAX_CLOUD_PERCENT = int(os.getenv("MAX_CLOUD_PERCENT", "100"))
SCALE_M = int(os.getenv("SCALE_M", "10"))

MAX_GRID = int(os.getenv("MAX_GRID", "30000"))
MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", "50331648"))  # 48MB

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "6"))
BASE_SLEEP = float(os.getenv("BASE_SLEEP", "2.0"))
MAX_SLEEP = float(os.getenv("MAX_SLEEP", "120.0"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "300"))

DATES_DIR = os.getenv("DATES_DIR", "/home/temur/Documents/Work/goo/dates")
RUN_STATE_FILE = os.getenv("RUN_STATE_FILE", "RUN_STATE.json")


# ---------------- utils ----------------

def _backoff_sleep(attempt: int) -> None:
    sleep_s = min(MAX_SLEEP, BASE_SLEEP * (2 ** (attempt - 1)))
    sleep_s += random.random()
    print(f"backoff {sleep_s:.1f} сек...")
    time.sleep(sleep_s)


def _looks_like_tiff(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(4)
        return head in (b"II*\x00", b"MM\x00*")
    except Exception:
        return False


def _looks_like_zip(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(2)
        return head == b"PK"
    except Exception:
        return False


def _looks_like_json_or_html(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(300).lstrip()
        low = head.lower()
        return head.startswith(b"{") or low.startswith(b"<!doctype") or low.startswith(b"<html") or low.startswith(b"[")
    except Exception:
        return False


def _save_error_preview(path: str, preview_path: str) -> None:
    try:
        with open(path, "rb") as f:
            data = f.read(2000)
        # пробуем показать как текст (если это JSON/HTML)
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = repr(data)
        with open(preview_path, "w", encoding="utf-8") as out:
            out.write(text)
    except Exception:
        pass


def download_url_to_file(url: str, outfile: str) -> None:
    """
    Устойчивое скачивание:
    - гарантирует директорию для .part
    - ретраи + backoff
    - ловит truncated downloads
    - если пришёл ZIP — вытаскивает первый TIFF
    - если пришёл JSON/HTML — считает ошибкой и ретраит
    """
    os.makedirs(os.path.dirname(outfile), exist_ok=True)

    for attempt in range(1, MAX_RETRIES + 1):
        tmp = outfile + ".part"
        try:
            with requests.get(url, stream=True, timeout=HTTP_TIMEOUT) as r:
                if r.status_code in (429, 500, 502, 503, 504):
                    raise RequestException(f"{r.status_code} {r.reason} for url: {url}")
                r.raise_for_status()

                expected_len = r.headers.get("Content-Length")
                expected_len_int = int(expected_len) if expected_len and expected_len.isdigit() else None

                written = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)
                    f.flush()
                    os.fsync(f.fileno())

                if expected_len_int is not None and written < expected_len_int:
                    raise RuntimeError(f"download truncated: got {written} bytes, expected {expected_len_int}")

                os.replace(tmp, outfile)

            # если пришёл JSON/HTML — это ошибка EE/квоты/и т.п.
            if _looks_like_json_or_html(outfile):
                preview = outfile + ".error_preview.txt"
                _save_error_preview(outfile, preview)
                raise RuntimeError(f"EE returned JSON/HTML instead of GeoTIFF (see {preview})")

            # иногда EE отдаёт ZIP даже для одного бэнда — распакуем
            if _looks_like_zip(outfile):
                with zipfile.ZipFile(outfile, "r") as z:
                    tif_names = [n for n in z.namelist() if n.lower().endswith((".tif", ".tiff"))]
                    if not tif_names:
                        raise RuntimeError("ZIP downloaded but no TIFF inside")
                    first = tif_names[0]
                    data = z.read(first)

                with open(tmp, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, outfile)

            if not _looks_like_tiff(outfile):
                preview = outfile + ".error_preview.txt"
                _save_error_preview(outfile, preview)
                raise RuntimeError(f"downloaded file is not a valid TIFF (bad header) (see {preview})")

            return

        except (RequestException, Timeout) as e:
            print(f"    сеть/сервис (попытка {attempt}/{MAX_RETRIES}): {e}")
            # чистим мусор
            for p in (tmp, outfile):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            if attempt < MAX_RETRIES:
                _backoff_sleep(attempt)
                continue
            raise

        except Exception as e:
            print(f"    ошибка (попытка {attempt}/{MAX_RETRIES}): {e}")
            for p in (tmp, outfile):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            if attempt < MAX_RETRIES:
                _backoff_sleep(attempt)
                continue
            raise


# ---------------- Sentinel-2 masking & composites ----------------

def mask_s2_sr_clouds(img: ee.Image) -> ee.Image:
    """
    Маска по SCL + QA60 (облака/циррус).
    """
    scl = img.select("SCL")
    scl_mask = (
        scl.neq(3)   # cloud shadow
        .And(scl.neq(8))   # cloud medium prob
        .And(scl.neq(9))   # cloud high prob
        .And(scl.neq(10))  # thin cirrus
        .And(scl.neq(11))  # snow/ice
    )

    qa = img.select("QA60")
    cloud_bit = 1 << 10
    cirrus_bit = 1 << 11
    qa_mask = qa.bitwiseAnd(cloud_bit).eq(0).And(qa.bitwiseAnd(cirrus_bit).eq(0))

    return img.updateMask(scl_mask.And(qa_mask))


def _collection(region: ee.Geometry, start: str, end: str) -> ee.ImageCollection:
    return (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(region)
        .filterDate(start, end)
        .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", MAX_CLOUD_PERCENT))
        .map(mask_s2_sr_clouds)
    )


def build_window_composite(region: ee.Geometry, center_day: datetime, window_days: int) -> Optional[ee.Image]:
    start = (center_day - timedelta(days=window_days)).strftime("%Y-%m-%d")
    end = (center_day + timedelta(days=window_days + 1)).strftime("%Y-%m-%d")

    col = _collection(region, start, end)
    if col.size().getInfo() == 0:
        return None

    return col.median().clip(region)


def build_filled_composite(
    region: ee.Geometry,
    target_day: datetime,
    win_main: int,
    win_fill: int,
    hole_heal_m: int = 0,
    final_force_unmask_zero: bool = True,
) -> Optional[ee.Image]:
    main = build_window_composite(region, target_day, win_main)
    if main is None:
        return None

    if win_fill > win_main:
        fill = build_window_composite(region, target_day, win_fill)
        filled = main.unmask(fill) if fill is not None else main
    else:
        filled = main

    if hole_heal_m and hole_heal_m > 0:
        neigh = filled.focal_median(radius=hole_heal_m, units="meters")
        filled = filled.unmask(neigh)

    if final_force_unmask_zero:
        filled = filled.unmask(0)

    return filled


# ---------------- geometry / tiling ----------------

def meters_per_degree_at_lat(lat: float) -> Tuple[float, float]:
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat))
    return m_per_deg_lon, m_per_deg_lat


def estimate_pixels_for_bbox(left: float, bottom: float, right: float, top: float, scale_m: int) -> Tuple[int, int]:
    mid_lat = (bottom + top) / 2.0
    m_lon, m_lat = meters_per_degree_at_lat(mid_lat)
    width_m = (right - left) * m_lon
    height_m = (top - bottom) * m_lat
    px_w = int(math.ceil(width_m / scale_m))
    px_h = int(math.ceil(height_m / scale_m))
    return px_w, px_h


def split_bbox(left: float, bottom: float, right: float, top: float, ncols: int, nrows: int):
    dx = (right - left) / ncols
    dy = (top - bottom) / nrows
    tiles = []
    for r in range(nrows):
        for c in range(ncols):
            l = left + c * dx
            rr = left + (c + 1) * dx
            b = bottom + r * dy
            tt = bottom + (r + 1) * dy
            tiles.append((c, r, l, b, rr, tt))
    return tiles


def choose_tiling(left: float, bottom: float, right: float, top: float, scale_m: int, max_grid: int, max_request_bytes: int):
    px_w, px_h = estimate_pixels_for_bbox(left, bottom, right, top, scale_m)

    ncols = max(1, math.ceil(px_w / max_grid))
    nrows = max(1, math.ceil(px_h / max_grid))

    # оценка bytes per pixel (консервативно)
    bpp = 12
    safety = 0.85
    max_tile_pixels = int((max_request_bytes * safety) // bpp)
    max_side = int(math.floor(math.sqrt(max_tile_pixels)))

    FORCE_MAX_SIDE = 1792
    max_side = min(max_side, FORCE_MAX_SIDE, max_grid)

    ncols2 = max(1, math.ceil(px_w / max_side))
    nrows2 = max(1, math.ceil(px_h / max_side))

    ncols = max(ncols, ncols2)
    nrows = max(nrows, nrows2)

    tile_w = math.ceil(px_w / ncols)
    tile_h = math.ceil(px_h / nrows)
    est_bytes = tile_w * tile_h * bpp
    print(f"tile approx: {tile_w}x{tile_h}px, est_request≈{est_bytes/1024/1024:.1f}MB (limit {max_request_bytes/1024/1024:.1f}MB)")

    return ncols, nrows, px_w, px_h, max_side


def flatten_coords(coords: Any) -> List[Tuple[float, float]]:
    pts: List[Tuple[float, float]] = []

    def _walk(x: Any):
        if isinstance(x, (list, tuple)) and len(x) == 2 and all(isinstance(v, (int, float)) for v in x):
            pts.append((float(x[0]), float(x[1])))
            return
        if isinstance(x, (list, tuple)):
            for item in x:
                _walk(item)

    _walk(coords)
    return pts


def bbox_from_geometry(geom: Dict[str, Any]) -> Tuple[float, float, float, float]:
    pts = flatten_coords(geom.get("coordinates"))
    if not pts:
        raise ValueError("Не нашёл coordinates в geometry")
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return min(lons), min(lats), max(lons), max(lats)


def parse_ddmmyyyy(s: str) -> datetime:
    return datetime.strptime(s.strip(), "%d.%m.%Y")


# ---------------- geojson jobs ----------------

def _collect_dates_value(v: Any) -> List[str]:
    if not v:
        return []
    if isinstance(v, str):
        v = v.strip()
        return [v] if v else []
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, str):
                x = x.strip()
                if x:
                    out.append(x)
        return out
    return []


def build_jobs_from_geojson(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(f"GeoJSON должен быть объектом, а не {type(data)}: {path}")

    jobs: List[Dict[str, Any]] = []

    t = data.get("type")
    if t == "FeatureCollection":
        feats = data.get("features", [])
        for i, feat in enumerate(feats):
            if not isinstance(feat, dict):
                continue
            geom = feat.get("geometry")
            if not isinstance(geom, dict) or "type" not in geom:
                continue

            props = feat.get("properties", {}) if isinstance(feat.get("properties"), dict) else {}
            dates = _collect_dates_value(props.get("date"))
            if not dates:
                dates = _collect_dates_value(data.get("date"))

            fid = str(props.get("OBJECTID") or props.get("objectid") or i)

            for d in dates:
                jobs.append({"fid": fid, "date": d, "geom": geom, "props": props})

    elif t == "Feature":
        geom = data.get("geometry")
        if not isinstance(geom, dict) or "type" not in geom:
            raise ValueError(f"В файле {path} не найдена geometry")

        props = data.get("properties", {}) if isinstance(data.get("properties"), dict) else {}
        dates = _collect_dates_value(props.get("date")) or _collect_dates_value(data.get("date"))
        if not dates:
            raise ValueError(f"В файле {path} не найдено поле date")

        fid = str(props.get("OBJECTID") or props.get("objectid") or "0")
        for d in dates:
            jobs.append({"fid": fid, "date": d, "geom": geom, "props": props})

    else:
        geom = data
        if not isinstance(geom, dict) or "type" not in geom:
            raise ValueError(f"В файле {path} не найдена geometry")
        dates = _collect_dates_value(data.get("date"))
        if not dates:
            raise ValueError(f"В файле {path} не найдено поле date")
        for d in dates:
            jobs.append({"fid": "0", "date": d, "geom": geom, "props": {}})

    if not jobs:
        raise ValueError(f"В файле {path} не найдено ни одной задачи (нет dates/geometry)")

    return jobs


# ---------------- raster helpers ----------------

def is_valid_tif(path: str) -> bool:
    if not os.path.exists(path):
        return False
    if os.path.getsize(path) < 1024:
        return False
    if not _looks_like_tiff(path):
        return False
    try:
        with rasterio.open(path) as src:
            _ = src.read(1, out_shape=(1, 1))
        return True
    except Exception:
        return False


def download_band_tile(img: ee.Image, band: str, tile_region: ee.Geometry, out_tif: str):
    """
    ВАЖНО: используем scale вместо crsTransform — меньше шансов получить zip/json/битый ответ.
    """
    one = img.select([band])

    url = one.getDownloadURL({
        "region": tile_region,
        "scale": SCALE_M,
        "format": "GEO_TIFF",
    })

    download_url_to_file(url, out_tif)


def merge_one_band(tile_paths: List[str], out_path: str):
    tile_paths = [p for p in tile_paths if is_valid_tif(p)]
    if not tile_paths:
        raise RuntimeError(f"Нет валидных tiles для merge: {out_path}")

    srcs = [rasterio.open(p) for p in tile_paths]
    mosaic, out_trans = merge(srcs)

    out_meta = srcs[0].meta.copy()
    out_meta.update({
        "height": mosaic.shape[1],
        "width": mosaic.shape[2],
        "transform": out_trans,
        "count": 1,
        "compress": "lzw",
        "nodata": 0,
        "dtype": str(mosaic.dtype),
    })

    for s in srcs:
        s.close()

    with rasterio.open(out_path, "w", **out_meta) as dst:
        dst.write(mosaic[0], 1)


def stack_rgb(b4_path: str, b3_path: str, b2_path: str, out_rgb_path: str):
    with rasterio.open(b4_path) as r:
        meta = r.meta.copy()
        b4 = r.read(1)

    with rasterio.open(b3_path) as g:
        b3 = g.read(1)

    with rasterio.open(b2_path) as b:
        b2 = b.read(1)

    meta.update(count=3, compress="lzw", nodata=0)

    with rasterio.open(out_rgb_path, "w", **meta) as dst:
        dst.write(b4, 1)
        dst.write(b3, 2)
        dst.write(b2, 3)


def rgb_tif_to_jpg(tif_path: str, jpg_path: str):
    """
    Простая визуализация: растягиваем 0..3000 в 0..255.
    """
    with rasterio.open(tif_path) as src:
        arr = src.read([1, 2, 3]).astype(np.float32)

    arr = np.clip(arr, 0, 3000)
    arr = (arr / 3000.0) * 255.0
    arr = arr.astype(np.uint8)

    img = np.transpose(arr, (1, 2, 0))
    Image.fromarray(img).save(jpg_path, quality=95)


# ---------------- resume state ----------------

def load_state() -> dict:
    if not os.path.exists(RUN_STATE_FILE):
        return {}
    try:
        with open(RUN_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_state(st: dict) -> None:
    with open(RUN_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def clear_state() -> None:
    if os.path.exists(RUN_STATE_FILE):
        os.remove(RUN_STATE_FILE)


# ---------------- main processing ----------------

def process_job(
    base_name: str,
    job_i: int,
    total: int,
    job: Dict[str, Any],
    only_missing: bool,
    win_main: int,
    win_fill: int,
    hole_heal_m: int,
):
    fid = job["fid"]
    date_str = job["date"]
    geom = job["geom"]
    props = job.get("props", {}) or {}

    prod_desc = props.get("prodDesc")

    region = ee.Geometry(geom)
    left, bottom, right, top = bbox_from_geometry(geom)

    try:
        target_day = parse_ddmmyyyy(date_str)
    except Exception as e:
        print(f"⚠ job {job_i}/{total} | fid={fid} | bad date='{date_str}': {e}")
        return

    comp = build_filled_composite(
        region=region,
        target_day=target_day,
        win_main=win_main,
        win_fill=win_fill,
        hole_heal_m=hole_heal_m,
        final_force_unmask_zero=True,
    )
    if comp is None:
        print(f"⚠ job {job_i}/{total} | fid={fid} | {date_str}: нет данных (окно main=±{win_main}, fill=±{win_fill})")
        return

    target_tag = target_day.strftime("%Y%m%d")

    OUT_DIR = os.path.join(SAVE_PATH, f"S2_{base_name}_FID{fid}_{target_tag}_{SCALE_M}m")
    TILES_DIR = os.path.join(OUT_DIR, "tiles", target_tag)
    os.makedirs(TILES_DIR, exist_ok=True)

    MERGED_TIF = os.path.join(OUT_DIR, f"RGB_{base_name}_FID{fid}_{target_tag}_{SCALE_M}m_merged.tif")
    OUT_JPG = os.path.join(OUT_DIR, f"RGB_{base_name}_FID{fid}_{target_tag}_{SCALE_M}m.jpg")

    print(f"\n--- job {job_i}/{total} | fid={fid} | target {date_str} | composite main=±{win_main}d fill=±{win_fill}d ---")
    if prod_desc:
        print(f"prodDesc: {prod_desc}")

    ncols, nrows, px_w, px_h, max_side = choose_tiling(left, bottom, right, top, SCALE_M, MAX_GRID, MAX_REQUEST_BYTES)
    print(f"bbox: ({left:.5f},{bottom:.5f})..({right:.5f},{top:.5f})")
    print(f"bbox pixels @ {SCALE_M}m: {px_w}x{px_h}")
    print(f"tiling: {ncols} x {nrows} | max_tile_side≈{max_side}px")

    tiles = split_bbox(left, bottom, right, top, ncols, nrows)
    band_to_paths: Dict[str, List[str]] = {"B4": [], "B3": [], "B2": []}

    missing_count = 0

    for c, r, l, b, rr, tt in tiles:
        tile_region = ee.Geometry.Rectangle([l, b, rr, tt])

        for band in ("B4", "B3", "B2"):
            tile_name = f"{band}_c{c}_r{r}.tif"
            out_tif = os.path.join(TILES_DIR, tile_name)
            band_to_paths[band].append(out_tif)

            if is_valid_tif(out_tif):
                continue

            if os.path.exists(out_tif):
                try:
                    os.remove(out_tif)
                except Exception:
                    pass

            missing_count += 1
            print(f"  ⬇ {tile_name}" + (" (missing)" if only_missing else ""))

            try:
                download_band_tile(comp, band, tile_region, out_tif)
                if not is_valid_tif(out_tif):
                    raise RuntimeError("скачалось, но tif не читается (битый)")
                print("    ✔ saved")
            except Exception as e:
                print(f"    ❌ tile failed: {tile_name}: {e}")

    if only_missing and missing_count == 0 and os.path.exists(MERGED_TIF) and os.path.exists(OUT_JPG):
        print("✅ already complete (tiles+merged+jpg), skip")
        return

    b4_merged = os.path.join(OUT_DIR, f"B4_merged_{target_tag}.tif")
    b3_merged = os.path.join(OUT_DIR, f"B3_merged_{target_tag}.tif")
    b2_merged = os.path.join(OUT_DIR, f"B2_merged_{target_tag}.tif")

    if not is_valid_tif(b4_merged):
        print("merge B4...")
        merge_one_band(band_to_paths["B4"], b4_merged)
    if not is_valid_tif(b3_merged):
        print("merge B3...")
        merge_one_band(band_to_paths["B3"], b3_merged)
    if not is_valid_tif(b2_merged):
        print("merge B2...")
        merge_one_band(band_to_paths["B2"], b2_merged)

    if not is_valid_tif(MERGED_TIF):
        print("stack RGB ->", MERGED_TIF)
        stack_rgb(b4_merged, b3_merged, b2_merged, MERGED_TIF)

    if not os.path.exists(OUT_JPG) or os.path.getsize(OUT_JPG) < 1024:
        print("tif -> jpg ->", OUT_JPG)
        rgb_tif_to_jpg(MERGED_TIF, OUT_JPG)

    print("DONE:", OUT_DIR)
    print("RGB GeoTIFF:", MERGED_TIF)
    print("JPEG:", OUT_JPG)


def process_one_geojson(path: str, only_missing: bool, win_main: int, win_fill: int, hole_heal_m: int, resume: bool):
    base_name = os.path.splitext(os.path.basename(path))[0]
    jobs = build_jobs_from_geojson(path)

    fids = sorted(set(j["fid"] for j in jobs))
    unique_dates = sorted(set(j["date"] for j in jobs))

    print(f"\n=== {base_name} | features={len(fids)} | jobs={len(jobs)} | unique_dates={len(unique_dates)} ===")
    print("dates(unique):", ", ".join(unique_dates))

    st = load_state() if resume else {}
    start_job = 1

    if resume and st.get("file") == os.path.basename(path) and isinstance(st.get("job_i"), int):
        start_job = max(1, int(st["job_i"]))
        print(f"🔁 RESUME: continue from job {start_job}/{len(jobs)} for {st.get('file')}")

    for job_i, job in enumerate(jobs, start=1):
        if job_i < start_job:
            continue

        if resume:
            save_state({"file": os.path.basename(path), "job_i": job_i})

        try:
            process_job(
                base_name, job_i, len(jobs), job,
                only_missing=only_missing,
                win_main=win_main,
                win_fill=win_fill,
                hole_heal_m=hole_heal_m,
            )
        except KeyboardInterrupt:
            print("\n⛔ Interrupted. Можно просто запустить снова — продолжит с этого job.")
            raise
        except Exception as e:
            print(f"❌ job {job_i}/{len(jobs)} failed: {e}")
            continue

    if resume:
        clear_state()


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--only-missing", action="store_true", help="Скачивать только отсутствующие/битые tiles и пересобрать")
    parser.add_argument("--no-resume", action="store_true", help="Не использовать resume (RUN_STATE.json)")

    parser.add_argument("--win-main", type=int, default=3, help="Основное окно композита ±N дней")
    parser.add_argument("--win-fill", type=int, default=15, help="Окно заполнения дыр ±N дней")
    parser.add_argument("--hole-heal-m", type=int, default=0, help="Лечить мелкие дырки focal_median радиусом в метрах (30/60)")

    args = parser.parse_args()

    if not PROJECT_ID:
        raise RuntimeError("PROJECT_ID не задан в .env")

    if not os.path.isdir(DATES_DIR):
        raise RuntimeError(f"DATES_DIR не существует: {DATES_DIR}")

    ee.Initialize(project=PROJECT_ID)

    files = []
    for fn in os.listdir(DATES_DIR):
        if fn.lower().endswith((".geojson", ".json")):
            files.append(os.path.join(DATES_DIR, fn))

    if not files:
        print(f"⚠ В папке {DATES_DIR} нет .geojson/.json файлов")
        return

    files.sort()
    print(f"Found {len(files)} geojson files in {DATES_DIR}")
    print(f"Settings: SCALE_M={SCALE_M}, MAX_CLOUD_PERCENT={MAX_CLOUD_PERCENT}, win_main=±{args.win_main}, win_fill=±{args.win_fill}, hole_heal_m={args.hole_heal_m}")

    for p in files:
        try:
            process_one_geojson(
                p,
                only_missing=args.only_missing,
                win_main=max(0, int(args.win_main)),
                win_fill=max(0, int(args.win_fill)),
                hole_heal_m=max(0, int(args.hole_heal_m)),
                resume=(not args.no_resume),
            )
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"❌ error for {p}: {e}")


if __name__ == "__main__":
    main()
