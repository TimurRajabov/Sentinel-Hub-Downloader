
import os
import re
import json
import random
import time
import math
import argparse
import zipfile
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import ee
import requests
from dotenv import load_dotenv
from requests.exceptions import RequestException, Timeout

import numpy as np
import rasterio
from rasterio.merge import merge
from PIL import Image

load_dotenv()

PROJECT_ID = os.getenv("PROJECT_ID")
SAVE_PATH = os.getenv("SAVE_PATH", "/data")

MAX_CLOUD_PERCENT = int(os.getenv("MAX_CLOUD_PERCENT", "100"))
SCALE_M = int(os.getenv("SCALE_M", "10"))

MAX_GRID = int(os.getenv("MAX_GRID", "30000"))
MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", "50331648"))

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "6"))
BASE_SLEEP = float(os.getenv("BASE_SLEEP", "2.0"))
MAX_SLEEP = float(os.getenv("MAX_SLEEP", "120.0"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "300"))

DATES_DIR = os.getenv("DATES_DIR", "/media/ulugbek/LOCAL1/Timur/goo/geojson")
RUN_STATE_FILE = os.getenv("RUN_STATE_FILE", "RUN_STATE.json")

MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "gee-downloader/1.0"})

def safe_filename(s: str) -> str:
    s = (s or "").strip()
    s = s.replace(" ", "_")
    s = re.sub(r"[^0-9A-Za-zА-Яа-я._-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s

def _backoff_sleep(attempt: int) -> None:
    sleep_s = min(MAX_SLEEP, BASE_SLEEP * (2 ** (attempt - 1)))
    sleep_s += random.random()
    print(f"backoff {sleep_s:.1f} сек...")
    time.sleep(sleep_s)

def _looks_like_tiff(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(4)
        # Classic TIFF: II*\x00 (LE) or MM\x00* (BE)
        # BigTIFF:      II+\x00 (LE) or MM\x00+ (BE)
        return head in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
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
            head = f.read(400).lstrip()
        low = head.lower()
        return (
            head.startswith(b"{")
            or head.startswith(b"[")
            or low.startswith(b"<!doctype")
            or low.startswith(b"<html")
        )
    except Exception:
        return False

def _save_error_preview(path: str, preview_path: str) -> None:
    try:
        with open(path, "rb") as f:
            data = f.read(4000)
        text = data.decode("utf-8", errors="replace")
        with open(preview_path, "w", encoding="utf-8") as out:
            out.write(text)
    except Exception:
        pass

def is_probably_valid_tif(path: str) -> bool:
    return (
        os.path.exists(path)
        and os.path.getsize(path) > 4096
        and _looks_like_tiff(path)
    )

def is_valid_tif(path: str) -> bool:
    if not is_probably_valid_tif(path):
        return False
    try:
        with rasterio.open(path) as src:
            _ = src.read(1, out_shape=(1, 1))
        return True
    except Exception:
        return False

def download_url_to_file(url: str, outfile: str) -> None:
    out_dir = os.path.dirname(outfile)

    for attempt in range(1, MAX_RETRIES + 1):
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        tmp = outfile + ".part"
        try:
            with SESSION.get(url, stream=True, timeout=HTTP_TIMEOUT) as r:
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

                if expected_len_int is not None and written < expected_len_int:
                    raise RuntimeError(f"download truncated: got {written} bytes, expected {expected_len_int}")

                os.replace(tmp, outfile)

            if _looks_like_json_or_html(outfile):
                preview = outfile + ".error_preview.txt"
                _save_error_preview(outfile, preview)
                raise RuntimeError(f"EE returned JSON/HTML instead of GeoTIFF (see {preview})")

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
                os.replace(tmp, outfile)

            if not _looks_like_tiff(outfile):
                preview = outfile + ".error_preview.txt"
                _save_error_preview(outfile, preview)
                raise RuntimeError(f"downloaded file is not a valid TIFF (bad header) (see {preview})")

            return

        except (RequestException, Timeout) as e:
            print(f"    сеть/сервис (попытка {attempt}/{MAX_RETRIES}): {e}")
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

def mask_s2_sr_clouds(img: ee.Image) -> ee.Image:
    scl = img.select("SCL")
    scl_mask = (
        scl.neq(3)
        .And(scl.neq(8))
        .And(scl.neq(9))
        .And(scl.neq(10))
        .And(scl.neq(11))
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

def build_window_composite(region: ee.Geometry, center_day: datetime, window_days: int) -> ee.Image:
    start = (center_day - timedelta(days=window_days)).strftime("%Y-%m-%d")
    end = (center_day + timedelta(days=window_days + 1)).strftime("%Y-%m-%d")
    col = _collection(region, start, end)
    return col.median().clip(region)

def build_filled_composite(
    region: ee.Geometry,
    target_day: datetime,
    win_main: int,
    win_fill: int,
    hole_heal_m: int = 0,
    final_force_unmask_zero: bool = True,
) -> Optional[ee.Image]:
    try:
        main = build_window_composite(region, target_day, win_main)
    except Exception:
        return None

    filled = main

    if win_fill > win_main:
        try:
            fill = build_window_composite(region, target_day, win_fill)
            filled = ee.Image(ee.Algorithms.If(
                main.bandNames().size().gt(0),
                ee.Image(ee.Algorithms.If(
                    fill.bandNames().size().gt(0),
                    fill.blend(main),
                    main,
                )),
                fill,
            ))
        except Exception:
            filled = main

    if hole_heal_m and hole_heal_m > 0:
        neigh = filled.focal_median(radius=hole_heal_m, units="meters")
        filled = ee.Image(ee.Algorithms.If(
            filled.bandNames().size().gt(0),
            neigh.blend(filled),
            filled,
        ))

    if final_force_unmask_zero:
        filled = ee.Image(ee.Algorithms.If(
            filled.bandNames().size().gt(0),
            filled.unmask(0),
            filled,
        ))

    return filled

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
            bb = bottom + r * dy
            tt = bottom + (r + 1) * dy
            tiles.append((c, r, l, bb, rr, tt))
    return tiles

def choose_tiling(left: float, bottom: float, right: float, top: float, scale_m: int, max_grid: int, max_request_bytes: int):
    px_w, px_h = estimate_pixels_for_bbox(left, bottom, right, top, scale_m)

    bytes_per_pixel = 3 * 2
    overhead_factor = 1.8
    max_tile_pixels = int(max_request_bytes / (bytes_per_pixel * overhead_factor))
    max_side = int(math.floor(math.sqrt(max_tile_pixels)))

    force_max_side = 1792
    max_side = min(max_side, force_max_side, max_grid)

    ncols = max(1, math.ceil(px_w / max_side))
    nrows = max(1, math.ceil(px_h / max_side))

    tile_w = math.ceil(px_w / ncols)
    tile_h = math.ceil(px_h / nrows)
    est_bytes = int(tile_w * tile_h * bytes_per_pixel * overhead_factor)

    print(
        f"tile approx: {tile_w}x{tile_h}px, "
        f"est_request≈{est_bytes / 1024 / 1024:.1f}MB "
        f"(limit {max_request_bytes / 1024 / 1024:.1f}MB)"
    )

    return ncols, nrows, px_w, px_h, max_side

def parse_ddmmyyyy(s: str) -> datetime:
    return datetime.strptime(s.strip(), "%d.%m.%Y")

def utm_epsg_for_lonlat(lon: float, lat: float) -> int:
    zone = int((lon + 180) // 6) + 1
    return (32600 + zone) if lat >= 0 else (32700 + zone)

def pick_job_crs_from_bbox(left: float, bottom: float, right: float, top: float) -> str:
    lon = (left + right) / 2.0
    lat = (bottom + top) / 2.0
    epsg = utm_epsg_for_lonlat(lon, lat)
    return f"EPSG:{epsg}"

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

def _match_shape_name(props: Dict[str, Any], shape_name_filter: str) -> bool:
    if not shape_name_filter:
        return True

    filter_val = shape_name_filter.strip().lower()

    candidates = [
        props.get("shapeName"),
        props.get("shape_name"),
        props.get("name"),
        props.get("NAME_1"),
        props.get("region"),
    ]

    for val in candidates:
        if isinstance(val, str) and val.strip().lower() == filter_val:
            return True

    return False

def build_jobs_from_geojson(path: str, shape_name_filter: str = "", date_override: str = "") -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(f"GeoJSON должен быть объектом, а не {type(data)}: {path}")

    jobs: List[Dict[str, Any]] = []
    top_level_dates = _collect_dates_value(data.get("date"))
    override_dates = _collect_dates_value(date_override)

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

            if not _match_shape_name(props, shape_name_filter):
                continue

            dates = _collect_dates_value(props.get("date")) or top_level_dates or override_dates
            if not dates:
                continue

            fid = str(props.get("OBJECTID") or props.get("objectid") or i)
            raw_name = (
                props.get("name")
                or props.get("shapeName")
                or props.get("shape_name")
                or props.get("NAME_1")
                or feat.get("name")
                or ""
            )
            name = safe_filename(str(raw_name)) if raw_name else ""

            for d in dates:
                jobs.append({
                    "fid": fid,
                    "name": name,
                    "date": d,
                    "geom": geom,
                    "props": props,
                })

    elif t == "Feature":
        geom = data.get("geometry")
        if not isinstance(geom, dict) or "type" not in geom:
            raise ValueError(f"В файле {path} не найдена geometry")

        props = data.get("properties", {}) if isinstance(data.get("properties"), dict) else {}

        if not _match_shape_name(props, shape_name_filter):
            return []

        dates = _collect_dates_value(props.get("date")) or top_level_dates or override_dates
        if not dates:
            raise ValueError(f"В файле {path} не найдено поле date и не передан --date")

        fid = str(props.get("OBJECTID") or props.get("objectid") or "0")
        raw_name = (
            props.get("name")
            or props.get("shapeName")
            or props.get("shape_name")
            or props.get("NAME_1")
            or data.get("name")
            or ""
        )
        name = safe_filename(str(raw_name)) if raw_name else ""

        for d in dates:
            jobs.append({
                "fid": fid,
                "name": name,
                "date": d,
                "geom": geom,
                "props": props,
            })

    else:
        geom = data
        if not isinstance(geom, dict) or "type" not in geom:
            raise ValueError(f"В файле {path} не найдена geometry")

        dates = top_level_dates or override_dates
        if not dates:
            raise ValueError(f"В файле {path} не найдено поле date и не передан --date")

        raw_name = data.get("name") or ""
        name = safe_filename(str(raw_name)) if raw_name else ""

        for d in dates:
            jobs.append({
                "fid": "0",
                "name": name,
                "date": d,
                "geom": geom,
                "props": {},
            })

    if not jobs:
        raise ValueError(
            f"В файле {path} не найдено ни одной задачи "
            f"(нет date и не передан --date, нет geometry, либо shapeName='{shape_name_filter}' не найден)"
        )

    return jobs

def is_request_too_large_error(msg: str) -> bool:
    if not msg:
        return False
    m = msg.lower()
    return (
        "total request size" in m
        and "must be less than or equal to" in m
    )

def split_tile_bbox(l: float, b: float, rr: float, tt: float):
    mx = (l + rr) / 2.0
    my = (b + tt) / 2.0
    return [
        (l,  b,  mx, my),
        (mx, b,  rr, my),
        (l,  my, mx, tt),
        (mx, my, rr, tt),
    ]

def merge_multiband_to_path(tile_paths: List[str], out_path: str):
    tile_paths = [p for p in tile_paths if is_valid_tif(p)]
    if not tile_paths:
        raise RuntimeError(f"Нет валидных RGB tiles для merge: {out_path}")

    srcs = [rasterio.open(p) for p in tile_paths]
    try:
        mosaic, out_trans = merge(srcs)

        out_meta = srcs[0].meta.copy()
        out_meta.update({
            "height": mosaic.shape[1],
            "width": mosaic.shape[2],
            "transform": out_trans,
            "count": mosaic.shape[0],
            "compress": "lzw",
            "nodata": 0,
            "dtype": str(mosaic.dtype),
            "BIGTIFF": "YES",
        })

        with rasterio.open(out_path, "w", **out_meta) as dst:
            dst.write(mosaic)
    finally:
        for s in srcs:
            s.close()

def download_rgb_tile_adaptive(
    img: ee.Image,
    l: float,
    b: float,
    rr: float,
    tt: float,
    out_tif: str,
    job_crs: str,
    depth: int = 0,
    max_depth: int = 4,
    bands: Optional[List[str]] = None,
) -> None:
    """
    Пытается скачать tile с указанными каналами.
    Если запрос слишком большой для EE, делит bbox на 4 части,
    скачивает их рекурсивно и мержит обратно в один out_tif.
    """
    tile_region = ee.Geometry.Rectangle([l, b, rr, tt])

    try:
        download_rgb_tile(img, tile_region, out_tif, job_crs, bands=bands)
        if not is_valid_tif(out_tif):
            raise RuntimeError("скачалось, но tif не читается")
        return

    except Exception as e:
        msg = str(e)

        if not is_request_too_large_error(msg):
            raise

        if depth >= max_depth:
            raise RuntimeError(
                f"Tile всё ещё слишком большой даже после деления до depth={depth}: {msg}"
            )

        tmp_dir = out_tif + f".parts_d{depth}"
        os.makedirs(tmp_dir, exist_ok=True)

        child_paths = []
        parts = split_tile_bbox(l, b, rr, tt)

        for i, (cl, cb, crr, ctt) in enumerate(parts):
            child_out = os.path.join(tmp_dir, f"part_{i}.tif")
            download_rgb_tile_adaptive(
                img=img,
                l=cl,
                b=cb,
                rr=crr,
                tt=ctt,
                out_tif=child_out,
                job_crs=job_crs,
                depth=depth + 1,
                max_depth=max_depth,
                bands=bands,
            )
            child_paths.append(child_out)

        merge_multiband_to_path(child_paths, out_tif)

def _get_bands_for_product(product: str, custom_bands: Optional[List[str]] = None) -> Optional[List[str]]:
    """Возвращает список каналов для заданного продукта. None = NDVI (особый случай)."""
    if product == "RGB":
        return ["B4", "B3", "B2"]
    elif product == "RGB+NIR":
        return ["B4", "B3", "B2", "B8"]
    elif product == "NDVI":
        return None
    elif product == "Custom" and custom_bands:
        return custom_bands
    return ["B4", "B3", "B2"]

def download_rgb_tile(img: ee.Image, tile_region: ee.Geometry, out_tif: str, job_crs: str,
                      bands: Optional[List[str]] = None):
    if bands is None:
        bands = ["B4", "B3", "B2"]
    selected = img.select(bands)
    url = selected.getDownloadURL({
        "region": tile_region,
        "crs": job_crs,
        "scale": SCALE_M,
        "format": "GEO_TIFF",
    })
    download_url_to_file(url, out_tif)

def download_ndvi_tile(img: ee.Image, tile_region: ee.Geometry, out_tif: str, job_crs: str):
    ndvi = img.normalizedDifference(["B8", "B4"]).rename("NDVI")
    url = ndvi.getDownloadURL({
        "region": tile_region,
        "crs": job_crs,
        "scale": SCALE_M,
        "format": "GEO_TIFF",
    })
    download_url_to_file(url, out_tif)

def merge_multiband(tile_paths: List[str], out_path: str):
    tile_paths = [p for p in tile_paths if is_valid_tif(p)]
    if not tile_paths:
        raise RuntimeError(f"Нет валидных RGB tiles для merge: {out_path}")

    srcs = [rasterio.open(p) for p in tile_paths]
    try:
        mosaic, out_trans = merge(srcs)

        out_meta = srcs[0].meta.copy()
        out_meta.update({
            "height": mosaic.shape[1],
            "width": mosaic.shape[2],
            "transform": out_trans,
            "count": mosaic.shape[0],
            "compress": "lzw",
            "nodata": 0,
            "dtype": str(mosaic.dtype),
            "BIGTIFF": "YES",
        })

        with rasterio.open(out_path, "w", **out_meta) as dst:
            dst.write(mosaic)
    finally:
        for s in srcs:
            s.close()

def rgb_tif_to_jpg(tif_path: str, jpg_path: str):
    with rasterio.open(tif_path) as src:
        arr = src.read([1, 2, 3]).astype(np.float32)

    arr = np.clip(arr, 0, 3000)
    arr = (arr / 3000.0) * 255.0
    arr = arr.astype(np.uint8)

    img = np.transpose(arr, (1, 2, 0))
    Image.fromarray(img).save(jpg_path, quality=95)

def ndvi_tif_to_jpg(tif_path: str, jpg_path: str):
    """NDVI [-1..1] → цветная визуализация (красный=низкий, зелёный=высокий)."""
    with rasterio.open(tif_path) as src:
        arr = src.read(1).astype(np.float32)

    arr = np.clip(arr, -1.0, 1.0)
    norm = (arr + 1.0) / 2.0

    r = np.clip((1.0 - norm) * 255, 0, 255).astype(np.uint8)
    g = np.clip(norm * 255, 0, 255).astype(np.uint8)
    b = np.zeros_like(r)

    img = np.stack([r, g, b], axis=2)
    Image.fromarray(img).save(jpg_path, quality=95)

def download_one_tile(
    comp: ee.Image,
    c: int,
    r: int,
    l: float,
    b: float,
    rr: float,
    tt: float,
    tiles_dir: str,
    job_crs: str,
    product: str = "RGB",
    custom_bands: Optional[List[str]] = None,
) -> Tuple[str, bool, Optional[str]]:
    tile_name = f"{product}_c{c}_r{r}.tif"
    out_tif = os.path.join(tiles_dir, tile_name)

    if is_valid_tif(out_tif):
        return out_tif, False, None

    if os.path.exists(out_tif):
        try:
            os.remove(out_tif)
        except Exception:
            pass

    try:
        tile_region = ee.Geometry.Rectangle([l, b, rr, tt])
        if product == "NDVI":
            download_ndvi_tile(comp, tile_region, out_tif, job_crs)
        else:
            bands = _get_bands_for_product(product, custom_bands)
            download_rgb_tile_adaptive(
                img=comp,
                l=l,
                b=b,
                rr=rr,
                tt=tt,
                out_tif=out_tif,
                job_crs=job_crs,
                depth=0,
                max_depth=4,
                bands=bands,
            )
        if not is_valid_tif(out_tif):
            raise RuntimeError("скачалось, но tif не читается")
        return out_tif, True, None
    except Exception as e:
        return out_tif, True, str(e)

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

def process_job(
    base_name: str,
    job_i: int,
    total: int,
    job: Dict[str, Any],
    only_missing: bool,
    win_main: int,
    win_fill: int,
    hole_heal_m: int,
    product: str = "RGB",
    custom_bands: Optional[List[str]] = None,
):
    fid = job["fid"]
    name = safe_filename(str(job.get("name") or ""))
    date_str = job["date"]
    geom = job["geom"]
    props = job.get("props", {}) or {}

    prod_desc = props.get("prodDesc")
    shape_name = props.get("shapeName") or props.get("NAME_1") or props.get("name")

    region = ee.Geometry(geom)
    left, bottom, right, top = bbox_from_geometry(geom)
    job_crs = pick_job_crs_from_bbox(left, bottom, right, top)

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
    id_part = name if name else f"FID{fid}"

    out_dir = os.path.join(SAVE_PATH, f"S2_{base_name}_{id_part}_{target_tag}_{SCALE_M}m")
    tiles_dir = os.path.join(out_dir, "tiles", target_tag)
    os.makedirs(tiles_dir, exist_ok=True)

    prod_tag = product if product != "Custom" else ("Custom_" + "_".join(custom_bands or []))
    merged_tif = os.path.join(out_dir, f"{prod_tag}_{base_name}_{id_part}_{target_tag}_{SCALE_M}m_merged.tif")
    out_jpg = os.path.join(out_dir, f"{prod_tag}_{base_name}_{id_part}_{target_tag}_{SCALE_M}m.jpg")

    print(f"\n--- job {job_i}/{total} | fid={fid} | target {date_str} | composite main=±{win_main}d fill=±{win_fill}d ---")
    if name:
        print(f"name: {name}")
    if shape_name:
        print(f"shape: {shape_name}")
    if prod_desc:
        print(f"prodDesc: {prod_desc}")
    print(f"export CRS (UTM): {job_crs}")

    ncols, nrows, px_w, px_h, max_side = choose_tiling(left, bottom, right, top, SCALE_M, MAX_GRID, MAX_REQUEST_BYTES)
    print(f"bbox: ({left:.5f},{bottom:.5f})..({right:.5f},{top:.5f})")
    print(f"bbox pixels @ {SCALE_M}m (approx): {px_w}x{px_h}")
    print(f"tiling: {ncols} x {nrows} | max_tile_side≈{max_side}px")

    tiles = split_bbox(left, bottom, right, top, ncols, nrows)

    if only_missing and is_valid_tif(merged_tif) and os.path.exists(out_jpg) and os.path.getsize(out_jpg) > 1024:
        all_tiles_ok = True
        for c, r, *_ in tiles:
            tile_path = os.path.join(tiles_dir, f"{product}_c{c}_r{r}.tif")
            if not is_valid_tif(tile_path):
                all_tiles_ok = False
                break
        if all_tiles_ok:
            print("✅ already complete (tiles+merged+jpg), skip")
            return

    tile_paths: List[str] = []
    missing_count = 0

    workers = max(1, MAX_WORKERS)
    print(f"download workers: {workers}")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = []
        for c, r, l, b, rr, tt in tiles:
            futures.append(
                ex.submit(
                    download_one_tile,
                    comp, c, r, l, b, rr, tt,
                    tiles_dir, job_crs, product, custom_bands
                )
            )

        for fut in as_completed(futures):
            out_tif, was_missing, err = fut.result()
            tile_paths.append(out_tif)
            if was_missing:
                missing_count += 1
                print(f"  ⬇ {os.path.basename(out_tif)}")
            if err:
                print(f"    ❌ tile failed: {os.path.basename(out_tif)}: {err}")
            else:
                if was_missing:
                    print(f"    ✔ saved: {os.path.basename(out_tif)}")

    valid_tile_paths = [p for p in tile_paths if is_valid_tif(p)]
    if not valid_tile_paths:
        raise RuntimeError(f"Не удалось скачать ни одного валидного {product} tile")

    if only_missing and missing_count == 0 and os.path.exists(merged_tif) and os.path.exists(out_jpg):
        print("✅ already complete (tiles+merged+jpg), skip")
        return

    if not is_valid_tif(merged_tif):
        print(f"merge {product} tiles...")
        merge_multiband(valid_tile_paths, merged_tif)

    if not os.path.exists(out_jpg) or os.path.getsize(out_jpg) < 1024:
        print("tif -> jpg ->", out_jpg)
        if product == "NDVI":
            ndvi_tif_to_jpg(merged_tif, out_jpg)
        else:
            rgb_tif_to_jpg(merged_tif, out_jpg)

    print("DONE:", out_dir)
    print(f"{product} GeoTIFF:", merged_tif)
    print("JPEG:", out_jpg)

def process_one_geojson(
    path: str,
    only_missing: bool,
    win_main: int,
    win_fill: int,
    hole_heal_m: int,
    resume: bool,
    shape_name: str = "",
    date_override: str = "",
):
    base_name = os.path.splitext(os.path.basename(path))[0]
    jobs = build_jobs_from_geojson(path, shape_name_filter=shape_name, date_override=date_override)

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
                base_name=base_name,
                job_i=job_i,
                total=len(jobs),
                job=job,
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
    parser.add_argument("--hole-heal-m", type=int, default=0, help="Лечить мелкие дырки focal_median радиусом в метрах")

    parser.add_argument("--geojson-file", type=str, default="", help="Путь к конкретному geojson/json файлу")
    parser.add_argument("--shape-name", type=str, default="", help="Фильтр по региону, например 'Fergana Region'")
    parser.add_argument("--date", type=str, default="", help="Дата вручную в формате DD.MM.YYYY, например 13.03.2026")

    args = parser.parse_args()

    if not PROJECT_ID:
        raise RuntimeError("PROJECT_ID не задан в .env")

    if args.date:
        try:
            parse_ddmmyyyy(args.date)
        except Exception:
            raise RuntimeError(f"Неверный формат --date: {args.date}. Нужен DD.MM.YYYY")

    ee.Initialize(project=PROJECT_ID)

    files: List[str] = []

    if args.geojson_file:
        if not os.path.isfile(args.geojson_file):
            raise RuntimeError(f"Файл не найден: {args.geojson_file}")
        files = [args.geojson_file]
    else:
        if not os.path.isdir(DATES_DIR):
            raise RuntimeError(f"DATES_DIR не существует: {DATES_DIR}")

        for fn in os.listdir(DATES_DIR):
            if fn.lower().endswith((".geojson", ".json")):
                files.append(os.path.join(DATES_DIR, fn))

    if not files:
        print("⚠ Нет geojson/json файлов для обработки")
        return

    files.sort()

    print(f"Found {len(files)} geojson files")
    print(
        f"Settings: SCALE_M={SCALE_M}, "
        f"MAX_CLOUD_PERCENT={MAX_CLOUD_PERCENT}, "
        f"win_main=±{args.win_main}, "
        f"win_fill=±{args.win_fill}, "
        f"hole_heal_m={args.hole_heal_m}, "
        f"MAX_WORKERS={MAX_WORKERS}"
    )
    print(f"DATES_DIR={DATES_DIR}")

    if args.shape_name:
        print(f"shape filter: {args.shape_name}")
    if args.date:
        print(f"date override: {args.date}")

    for p in files:
        try:
            process_one_geojson(
                path=p,
                only_missing=args.only_missing,
                win_main=max(0, int(args.win_main)),
                win_fill=max(0, int(args.win_fill)),
                hole_heal_m=max(0, int(args.hole_heal_m)),
                resume=(not args.no_resume),
                shape_name=args.shape_name,
                date_override=args.date,
            )
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"❌ error for {p}: {e}")

if __name__ == "__main__":
    main()