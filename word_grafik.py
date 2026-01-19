# -*- coding: utf-8 -*-
import json
import os
import re
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

import matplotlib
import numpy as np
import rioxarray

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from osgeo import ogr, osr

ogr.UseExceptions()
osr.UseExceptions()

GAS_UNITS = {
    "CH4": "ppm",
    "CO": "mol/m²",
    "NO2": "mol/km²",
    "SO2": "mol/km²",
    "HCHO": "mol/km²",
    "O3": "mol/m²",
    "AERAI": "",
}


def _parse_date_from_filename(path: str) -> datetime:
    base = os.path.basename(path)
    m = re.search(r"(\d{4}-\d{2}-\d{2}|\d{8}|\d{2}-\d{2}-\d{4})", base)
    if not m:
        raise ValueError(f"Can't parse date from filename: {path}")

    d = m.group(1)
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(d, fmt).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
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
    name = (
        feat.GetField("region_nam") or feat.GetField("region_name") or str(parent_cod)
    )
    return feat, str(name)


def _srs_axis(srs: osr.SpatialReference) -> osr.SpatialReference:
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _geom_to_raster_geojson(
    geom: ogr.Geometry, vec_srs: osr.SpatialReference, ras_crs
) -> dict:
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
    by_day: Dict[datetime, str] = {}
    for dt, path in selected:
        day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        by_day[day] = path
    return sorted(by_day.items(), key=lambda x: x[0])


def _smooth_curve(dts: List[datetime], ys: np.ndarray, n_points: int = 400):
    x = mdates.date2num(dts).astype(float)
    y = ys.astype(float)

    order = np.argsort(x)
    x = x[order]
    y = y[order]

    uniq_x, uniq_y = [], []
    last = None
    acc = []
    for xi, yi in zip(x, y):
        if last is None or xi == last:
            acc.append(yi)
            last = xi
        else:
            uniq_x.append(last)
            uniq_y.append(float(np.mean(acc)))
            acc = [yi]
            last = xi
    if last is not None:
        uniq_x.append(last)
        uniq_y.append(float(np.mean(acc)))

    x = np.array(uniq_x, dtype=float)
    y = np.array(uniq_y, dtype=float)

    if x.size <= 1:
        return x, y

    x_new = np.linspace(x.min(), x.max(), int(max(50, n_points)))

    # 1) try SciPy cubic spline (best)
    try:
        from scipy.interpolate import make_interp_spline  # type: ignore

        if x.size >= 4:
            spl = make_interp_spline(x, y, k=3)
            y_new = spl(x_new)
        elif x.size == 3:
            spl = make_interp_spline(x, y, k=2)
            y_new = spl(x_new)
        else:
            y_new = np.interp(x_new, x, y)
        return x_new, y_new
    except Exception:
        pass

    # 2) fallback: polynomial fit (safe-ish)
    deg = 3 if x.size >= 4 else (2 if x.size == 3 else 1)
    try:
        coeff = np.polyfit(x, y, deg)
        y_new = np.polyval(coeff, x_new)
        return x_new, y_new
    except Exception:
        y_new = np.interp(x_new, x, y)
        return x_new, y_new


def _build_chart_png(
    gas: str,
    region_name: str,
    year: int,
    unit: str,
    points: List[Tuple[datetime, float]],
    lookback_days: int,
) -> bytes:
    dts = [dt for dt, _ in points]
    y = np.array([v for _, v in points], dtype=float)

    x_s, y_s = _smooth_curve(dts, y, n_points=500)

    fig = plt.figure(figsize=(12, 5.5), dpi=170)
    ax = fig.add_subplot(111)

    ax.plot(mdates.num2date(x_s), y_s, linewidth=2.7)

    ax.set_title(f"{gas} — {region_name} {year}")
    ax.set_ylabel(f"Mean ({unit})")
    ax.grid(True, alpha=0.25, linestyle="--", linewidth=0.8)

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m.%d"))
    if lookback_days == 7:
        ax.set_xticks(dts)
    elif lookback_days == 15:
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=2))
    else:
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=6, maxticks=15))

    fig.autofmt_xdate(rotation=0, ha="center")

    import io

    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def make_grafik(
    gas: str,
    date_str: str,
    parent_cod: int,
    rasters_root: str,
    mintaqa_shp: str,
    out_dir: str,
    lookback_days: int = 30,
) -> dict:
    gas = gas.upper()
    os.makedirs(out_dir, exist_ok=True)

    if lookback_days not in (7, 15, 30):
        lookback_days = 30

    end_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
        hour=0, minute=0, second=0, microsecond=0
    )
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
        if dt > end_dt or dt < start_dt:
            continue
        selected.append((dt, p))

    if not selected:
        raise RuntimeError(
            f"Нет tif в диапазоне {start_dt:%Y-%m-%d}..{end_dt:%Y-%m-%d} для {gas}"
        )

    selected = _dedupe_by_date(selected)

    points: List[Tuple[datetime, float]] = []
    for dt, tif_path in selected:
        da = rioxarray.open_rasterio(tif_path).squeeze()
        poly_geo = _geom_to_raster_geojson(geom, vec_srs, da.rio.crs)
        clipped = da.rio.clip([poly_geo], drop=True)
        points.append((dt, _compute_mean(gas, clipped.values.flatten())))

    png_bytes = _build_chart_png(
        gas=gas,
        region_name=region_name,
        year=year,
        unit=GAS_UNITS.get(gas, "unit"),
        points=points,
        lookback_days=lookback_days,
    )

    out_path = os.path.join(out_dir, f"{gas}_{date_str}_grafik.png")
    with open(out_path, "wb") as f:
        f.write(png_bytes)

    ds_vec = None
    return {"png": out_path, "region_name": region_name}
