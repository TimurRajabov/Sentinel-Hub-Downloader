# -*- coding: utf-8 -*-
import os
from datetime import datetime

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, Normalize
from osgeo import gdal, ogr, osr

gdal.UseExceptions()
ogr.UseExceptions()
osr.UseExceptions()

GAS_UNITS = {
    "CH4": "ppm",
    "CO": "mol/km²",
    "NO2": "mol/km²",
    "SO2": "mol/km²",
    "HCHO": "mol/km²",
    "O3": "mol/m²",
    "AERAI": "unitless",
}
LEGEND_SCALE = {"CH4": 1 / 1000.0}

# --- Render settings ---
MIN_PERCENT, MAX_PERCENT = 0.4, 99.6
HIST_BUCKETS = 16384
DISPLAY_INTERP = "bicubic"
ZOOM_PAD = 0.10

BASE_COLOR = "#111111"
BASE_W_RAYON = 0.5
BASE_W_MINTAQA = 1.0

SEL_COLOR = "#FFFFFF"
SEL_W = 2.0
LEGEND_TICKS = 3


def _spectrum_cmap():
    stops = [
        "#800080",
        "#4b0082",
        "#0000ff",
        "#00ffff",
        "#00ff00",
        "#ffff00",
        "#ffa500",
        "#ff0000",
    ]
    cols = [
        (int(s[1:3], 16) / 255, int(s[3:5], 16) / 255, int(s[5:7], 16) / 255)
        for s in stops
    ]
    return LinearSegmentedColormap.from_list("spectrum", cols, N=256)


def _percent_clip(arr, pmin, pmax, bins):
    v = arr[np.isfinite(arr)]
    lo, hi = float(np.nanmin(v)), float(np.nanmax(v))
    if hi == lo:
        return lo, hi
    hist, edges = np.histogram(v, bins=bins, range=(lo, hi))
    cdf = np.cumsum(hist).astype(np.float64)
    total = cdf[-1]
    i0 = int(np.searchsorted(cdf, total * (pmin / 100.0), side="left"))
    i1 = int(np.searchsorted(cdf, total * (pmax / 100.0), side="left"))
    i0 = int(np.clip(i0, 0, len(edges) - 2))
    i1 = int(np.clip(i1, 0, len(edges) - 2))
    return float(edges[i0]), float(edges[i1 + 1])


def _srs_axis(srs, fallback_epsg=None):
    if srs is None:
        srs = osr.SpatialReference()
        if fallback_epsg:
            srs.ImportFromEPSG(int(fallback_epsg))
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _find_raster(root, gas, date_str):
    datetime.strptime(date_str, "%Y-%m-%d")
    p = os.path.join(root, gas.upper(), f"{gas.upper()}_{date_str}.tif")
    if not os.path.exists(p):
        raise FileNotFoundError(p)
    return p


def _get_feature_by_parent_cod(lyr, parent_cod):
    if lyr.GetLayerDefn().GetFieldIndex("parent_cod") < 0:
        raise RuntimeError("В VECTOR_PATH нет поля 'parent_cod'")

    # числовой фильтр (если у вас строкой — поменяйте на кавычки)
    lyr.SetAttributeFilter(f"parent_cod = {int(parent_cod)}")
    feat = lyr.GetNextFeature()
    lyr.SetAttributeFilter(None)
    lyr.ResetReading()

    if feat is None:
        raise RuntimeError(f"parent_cod={parent_cod} не найден")
    return feat


def _layer_extent_in_raster_srs(lyr, ct):
    xmin, xmax, ymin, ymax = lyr.GetExtent()
    pts = [(xmin, ymin), (xmin, ymax), (xmax, ymin), (xmax, ymax)]
    xs, ys = [], []
    for x, y in pts:
        p = ogr.Geometry(ogr.wkbPoint)
        p.AddPoint(x, y)
        if ct:
            p.Transform(ct)
        xs.append(p.GetX())
        ys.append(p.GetY())
    return min(xs), max(xs), min(ys), max(ys)


def _plot_geom(ax, geom, inv_gt, color, lw, z):
    def draw_ring(ring):
        xs, ys = [], []
        for (x, y, *_) in ring.GetPoints():
            c, r = gdal.ApplyGeoTransform(inv_gt, x, y)
            xs.append(c)
            ys.append(r)
        ax.plot(xs, ys, color=color, linewidth=lw, zorder=z)

    t = geom.GetGeometryType()
    if t in (ogr.wkbPolygon, ogr.wkbPolygon25D):
        for i in range(geom.GetGeometryCount()):
            draw_ring(geom.GetGeometryRef(i))
    elif t in (ogr.wkbMultiPolygon, ogr.wkbMultiPolygon25D):
        for pi in range(geom.GetGeometryCount()):
            poly = geom.GetGeometryRef(pi)
            for ri in range(poly.GetGeometryCount()):
                draw_ring(poly.GetGeometryRef(ri))


def _add_legend(fig, ax, cmap, vmin_raw, vmax_raw, title, scale=1.0):
    ticks = (
        np.linspace(vmin_raw, vmax_raw, LEGEND_TICKS)
        if LEGEND_TICKS > 1
        else np.array([(vmin_raw + vmax_raw) / 2])
    )

    def fmt(x):
        return f"{(float(x)*scale):.6g}"

    fig.canvas.draw()
    bbox = ax.get_position()

    # легенда в белой полосе снизу (позиция относительно карты)
    w, h = 0.20, 0.02
    left = bbox.x0
    bottom = max(0.0, bbox.y0 - 0.03 - h)

    cax = fig.add_axes([left, bottom, w, h])
    sm = plt.cm.ScalarMappable(norm=Normalize(vmin=vmin_raw, vmax=vmax_raw), cmap=cmap)
    sm.set_array([])
    cb = fig.colorbar(sm, cax=cax, orientation="horizontal")
    cb.set_ticks(ticks)
    cb.set_ticklabels([fmt(t) for t in ticks])
    cb.outline.set_visible(False)
    cax.set_title(title, fontsize=10, pad=6)
    cax.tick_params(axis="x", labelsize=9, length=0)


def make_screens(
    gas: str,
    date_str: str,
    parent_cod: int,
    rasters_root: str,
    vector_path: str,
    base_vector_path: str,
    out_dir: str,
) -> dict:
    """
    Returns:
      {
        "rayon": path_to_png,
        "mintaqa": path_to_png,
        "raster": tif_path
      }
    """
    gas = gas.upper()
    os.makedirs(out_dir, exist_ok=True)

    tif_path = _find_raster(rasters_root, gas, date_str)
    ds = gdal.Open(tif_path)
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray().astype(np.float32)

    nodata = band.GetNoDataValue()
    if nodata is not None:
        arr[arr == nodata] = np.nan
    mb = band.GetMaskBand()
    if mb is not None:
        arr[mb.ReadAsArray() == 0] = np.nan

    vmin_raw, vmax_raw = float(np.nanmin(arr)), float(np.nanmax(arr))
    vmin_clip, vmax_clip = _percent_clip(arr, MIN_PERCENT, MAX_PERCENT, HIST_BUCKETS)
    den = (vmax_clip - vmin_clip) or 1.0
    norm = np.clip((arr - vmin_clip) / den, 0.0, 1.0)

    cmap = _spectrum_cmap()
    cmap.set_bad((0, 0, 0, 0))

    inv_gt = gdal.InvGeoTransform(ds.GetGeoTransform())

    ras_srs = osr.SpatialReference()
    wkt = ds.GetProjection()
    ras_srs.ImportFromWkt(wkt) if wkt else ras_srs.ImportFromEPSG(4326)
    ras_srs = _srs_axis(ras_srs)

    unit = GAS_UNITS.get(gas, "unit")
    scale = LEGEND_SCALE.get(gas, 1.0)
    legend_title = f"{gas} Konsentratsiyasi ({unit})"

    def render_one(mode: str, out_png: str, base_w: float, highlight: bool):
        fig, ax = plt.subplots(figsize=(10, 8))
        ax.imshow(norm, cmap=cmap, interpolation=DISPLAY_INTERP, zorder=0)

        vds = ogr.Open(vector_path)
        lyr = vds.GetLayer(0)
        vec_srs = _srs_axis(lyr.GetSpatialRef(), fallback_epsg=3857)
        ct = (
            osr.CoordinateTransformation(vec_srs, ras_srs)
            if not vec_srs.IsSame(ras_srs)
            else None
        )

        g_sel = None
        if mode == "feature":
            feat = _get_feature_by_parent_cod(lyr, parent_cod)
            g_sel = feat.GetGeometryRef().Clone()
            if ct:
                g_sel.Transform(ct)
            xmin2, xmax2, ymin2, ymax2 = g_sel.GetEnvelope()
        else:
            xmin2, xmax2, ymin2, ymax2 = _layer_extent_in_raster_srs(lyr, ct)

        padx, pady = (xmax2 - xmin2) * ZOOM_PAD, (ymax2 - ymin2) * ZOOM_PAD
        xmin2 -= padx
        xmax2 += padx
        ymin2 -= pady
        ymax2 += pady

        x0, y0 = gdal.ApplyGeoTransform(inv_gt, xmin2, ymin2)
        x1, y1 = gdal.ApplyGeoTransform(inv_gt, xmax2, ymax2)
        ax.set_xlim(min(x0, x1), max(x0, x1))
        ax.set_ylim(max(y0, y1), min(y0, y1))

        # base borders
        bds = ogr.Open(base_vector_path)
        blyr = bds.GetLayer(0)
        b_srs = _srs_axis(blyr.GetSpatialRef(), fallback_epsg=3857)
        bct = (
            osr.CoordinateTransformation(b_srs, ras_srs)
            if not b_srs.IsSame(ras_srs)
            else None
        )
        for bf in blyr:
            bg = bf.GetGeometryRef()
            if not bg:
                continue
            bg = bg.Clone()
            if bct:
                bg.Transform(bct)
            _plot_geom(ax, bg, inv_gt, BASE_COLOR, base_w, z=3)

        if highlight and g_sel is not None:
            _plot_geom(ax, g_sel, inv_gt, SEL_COLOR, SEL_W * 1.6, z=8)
            _plot_geom(ax, g_sel, inv_gt, SEL_COLOR, SEL_W, z=9)

        ax.axis("off")
        fig.subplots_adjust(bottom=0.18)
        _add_legend(fig, ax, cmap, vmin_raw, vmax_raw, legend_title, scale=scale)

        fig.savefig(out_png, dpi=200, bbox_inches="tight", pad_inches=0)
        plt.close(fig)

    out_rayon = os.path.join(out_dir, f"{gas}_{date_str}_rayon_screen.png")
    out_mintaqa = os.path.join(out_dir, f"{gas}_{date_str}_mintaqa_screen.png")

    render_one("feature", out_rayon, base_w=BASE_W_RAYON, highlight=True)
    render_one("layer", out_mintaqa, base_w=BASE_W_MINTAQA, highlight=False)

    return {"rayon": out_rayon, "mintaqa": out_mintaqa, "raster": tif_path}
