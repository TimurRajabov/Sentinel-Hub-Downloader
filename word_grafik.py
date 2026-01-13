# -*- coding: utf-8 -*-
import os
import re
import json
from datetime import datetime, timedelta
from typing import List, Tuple, Dict

import numpy as np
import rioxarray

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from osgeo import ogr, osr

ogr.UseExceptions()
osr.UseExceptions()

GAS_UNITS = {
    "CH4": "ppm",
    "CO": "mol/m²",
    "NO2": "mol/m²",
    "SO2": "mol/m²",
    "HCHO": "mol/m²",
    "O3": "mol/m²",
    "AERAI": "unitless",
}


def _parse_date_from_filename(path: str) -> datetime:
    base = os.path.basename(path)
    s = re.sub(r"\.tif{1,2}$", "", base, flags=re.IGNORECASE)
    d = s.split("_")[-1]
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(d, fmt).replace(hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            pass
    raise ValueError(f"Can't parse date from filename: {path}")


def _list_tiffs(rasters_root: str, gas: str) -> List[str]:
    folder = os.path.join(rasters_root, gas.upper())
    if not os.path.isdir(folder):
        return []
    return sorted(
        os.path.join(folder, f)
        for f in os.listdir(folder)
        if f.lower().endswith(".tif")
    )


def _open_layer(shp_path: str):
    ds = ogr.Open(shp_path)
    if ds is None:
        raise RuntimeError(f"Не могу открыть shp: {shp_path}")
    lyr = ds.GetLayer(0)
    if lyr is None:
        raise RuntimeError(f"Нет слоя в shp: {shp_path}")
    return ds, lyr


def _get_feature_and_name(lyr, parent_cod: int):
    lyr.SetAttributeFilter(f"parent_cod = {int(parent_cod)}")
    feat = lyr.GetNextFeature()
    lyr.SetAttributeFilter(None)
    lyr.ResetReading()
    if not feat:
        raise RuntimeError(f"parent_cod={parent_cod} не найден в shp")
    name = feat.GetField("region_nam") or feat.GetField("region_name") or str(parent_cod)
    return feat, str(name)


def _srs_axis(srs: osr.SpatialReference) -> osr.SpatialReference:
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _geom_to_raster_geojson(geom: ogr.Geometry, vec_srs: osr.SpatialReference, ras_crs) -> dict:
    ras_srs = osr.SpatialReference()
    if ras_crs is None:
        ras_srs.ImportFromEPSG(4326)
    else:
        epsg = ras_crs.to_epsg()
        if epsg is not None:
            ras_srs.ImportFromEPSG(int(epsg))
        else:
            try:
                ras_srs.ImportFromWkt(ras_crs.to_wkt())
            except Exception:
                ras_srs.ImportFromEPSG(4326)

    vec_srs = _srs_axis(vec_srs)
    ras_srs = _srs_axis(ras_srs)

    g2 = geom.Clone()
    if not vec_srs.IsSame(ras_srs):
        ct = osr.CoordinateTransformation(vec_srs, ras_srs)
        g2.Transform(ct)

    return json.loads(g2.ExportToJson())


def _compute_mean(gas: str, values: np.ndarray) -> float:
    v = values[np.isfinite(values)]
    v = v[(v < 1e20)]
    if v.size == 0:
        return 0.0
    raw = float(np.nanmean(v))

    if gas == "CH4":
        return round(raw / 1000.0, 3)
    if gas in ("NO2", "SO2", "HCHO"):
        return round(raw * 1e6, 3)

    return round(raw, 3)


def _dedupe_by_date(selected: List[Tuple[datetime, str]]) -> List[Tuple[datetime, str]]:
    """
    Убираем дубли по дате (если в папке несколько tif на одну дату).
    Берём один путь на дату (последний по сортировке путей).
    """
    by_day: Dict[datetime, str] = {}
    for dt, path in selected:
        day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        by_day[day] = path
    return sorted(by_day.items(), key=lambda x: x[0])


def _build_chart_png(gas: str, region_name: str, year: int, unit: str,
                     points: List[Tuple[datetime, float]], lookback_days: int) -> bytes:
    x = [dt for dt, _ in points]
    y = [v for _, v in points]

    fig = plt.figure(figsize=(12, 5.5), dpi=170)
    ax = fig.add_subplot(111)

    ax.plot(x, y, marker="o", linewidth=2)
    ax.set_title(f"{gas} — {region_name} {year}")
    ax.set_ylabel(f"Mean ({unit})")
    ax.grid(True, alpha=0.3)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m.%d"))

    # ---- Тики по вашему правилу ----
    if lookback_days == 7:
        # каждую дату (все точки)
        ax.set_xticks(x)
    elif lookback_days == 15:
        # каждые 2 дня
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=2))
    else:
        # 30 (или другое) — как раньше: до ~15 подписей
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=6, maxticks=15))

    fig.autofmt_xdate(rotation=0, ha="center")

    import io
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def make_grafik(gas: str, date_str: str, parent_cod: int,
                rasters_root: str, mintaqa_shp: str, out_dir: str,
                lookback_days: int = 30) -> dict:
    """
    Делает {gas}_{DATE}_grafik.png

    lookback_days: 30 / 15 / 7
      30 -> текущее поведение
      15 -> подписи каждые 2 дня
      7  -> подписи на каждую дату
    """
    gas = gas.upper()
    os.makedirs(out_dir, exist_ok=True)

    if lookback_days not in (7, 15, 30):
        lookback_days = 30

    end_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=0, minute=0, second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=int(lookback_days) - 1)

    year = end_dt.year

    ds_vec, lyr = _open_layer(mintaqa_shp)
    vec_srs = lyr.GetSpatialRef()
    if vec_srs is None:
        raise RuntimeError("У MINTAQA_SHP нет Spatial Reference (prj).")

    feat, region_name = _get_feature_and_name(lyr, parent_cod)
    geom = feat.GetGeometryRef()
    if geom is None:
        raise RuntimeError(f"У parent_cod={parent_cod} нет геометрии")

    tiffs = _list_tiffs(rasters_root, gas)
    if not tiffs:
        raise RuntimeError(f"Нет tif для {gas}")

    selected: List[Tuple[datetime, str]] = []
    for p in tiffs:
        try:
            dt = _parse_date_from_filename(p)
        except ValueError:
            continue

        if dt > end_dt:
            continue
        if dt < start_dt:
            continue

        selected.append((dt, p))

    if not selected:
        raise RuntimeError(f"Нет tif в диапазоне {start_dt:%Y-%m-%d}..{end_dt:%Y-%m-%d} для {gas}")

    # FIX: убираем дубли по датам (иначе точки могут повторяться)
    selected = _dedupe_by_date(selected)

    points: List[Tuple[datetime, float]] = []
    for dt, tif_path in selected:
        da = rioxarray.open_rasterio(tif_path).squeeze()
        poly_geo = _geom_to_raster_geojson(geom, vec_srs, da.rio.crs)
        clipped = da.rio.clip([poly_geo], drop=False)
        points.append((dt, _compute_mean(gas, clipped.values.flatten())))

    png_bytes = _build_chart_png(
        gas=gas,
        region_name=region_name,
        year=year,
        unit=GAS_UNITS.get(gas, "unit"),
        points=points,
        lookback_days=lookback_days
    )

    out_path = os.path.join(out_dir, f"{gas}_{date_str}_grafik.png")
    with open(out_path, "wb") as f:
        f.write(png_bytes)

    ds_vec = None
    return {"png": out_path, "region_name": region_name}
