# -*- coding: utf-8 -*-
import os
from datetime import datetime

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, ListedColormap, Normalize
from matplotlib.patches import Rectangle
from osgeo import gdal, ogr, osr

gdal.UseExceptions()
ogr.UseExceptions()
osr.UseExceptions()

GAS_UNITS = {
    "CH4": "ppm",
    "CO": "mol/m²",
    "NO2": "mol/m²",
    "SO2": "mol/m²",
    "HCHO": "mol/m²",
    "O3": "mol/m²",
    "AERAI": "",
}
LEGEND_SCALE = {"CH4": 1 / 1000}

DISPLAY_INTERP_MAIN = "bicubic"
DISPLAY_INTERP_ZERO = "bicubic"
ZOOM_PAD = 0.10

BASE_COLOR = "#111111"
BASE_W_RAYON = 0.5
BASE_W_MINTAQA = 1.0

SEL_COLOR = "#FFFFFF"
SEL_W = 2.0
LEGEND_TICKS = 3

ZERO_COLOR = "#ff00ff"

PCLIP_LOW = 2.0
PCLIP_HIGH = 98.0


def _spectrum_cmap():
    stops = [
        "#0b1a8f",
        "#0033ff",
        "#0080ff",
        "#00ffff",
        "#66ff00",
        "#ffff00",
        "#ff9900",
        "#ff0000",
    ]
    cols = [
        (int(s[1:3], 16) / 255, int(s[3:5], 16) / 255, int(s[5:7], 16) / 255)
        for s in stops
    ]
    return LinearSegmentedColormap.from_list("spectrum", cols, N=256)


def _srs_axis(srs, fallback_epsg=None):
    if srs is None:
        srs = osr.SpatialReference()
        if fallback_epsg:
            srs.ImportFromEPSG(int(fallback_epsg))
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _inv_gt(ds):
    inv = gdal.InvGeoTransform(ds.GetGeoTransform())
    if isinstance(inv, (tuple, list)) and len(inv) == 2:
        ok, inv_gt = inv
        if not ok:
            raise RuntimeError("InvGeoTransform failed")
        return inv_gt
    return inv


def _find_raster(root, gas, date_str):
    datetime.strptime(date_str, "%Y-%m-%d")
    folder = os.path.join(root, gas.upper())
    for p in (
        os.path.join(folder, f"{gas.upper()}_{date_str}.tif"),
        os.path.join(folder, f"{gas.upper()}_{date_str}_ADS.tif"),
    ):
        if os.path.exists(p):
            return p
    raise FileNotFoundError("Raster not found. Tried default names.")


def _get_feature_by_parent_cod(lyr, parent_cod):
    lyr.SetAttributeFilter(f"parent_cod = {int(parent_cod)}")
    feat = lyr.GetNextFeature()
    lyr.SetAttributeFilter(None)
    lyr.ResetReading()
    if feat is None:
        raise RuntimeError(f"parent_cod={parent_cod} not found")
    return feat


def _layer_extent_in_raster_srs(lyr, ct):
    xmin, xmax, ymin, ymax = lyr.GetExtent()
    pts = ((xmin, ymin), (xmin, ymax), (xmax, ymin), (xmax, ymax))
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

    gt = geom.GetGeometryType()
    if gt in (ogr.wkbPolygon, ogr.wkbPolygon25D):
        for i in range(geom.GetGeometryCount()):
            draw_ring(geom.GetGeometryRef(i))
    elif gt in (ogr.wkbMultiPolygon, ogr.wkbMultiPolygon25D):
        for pi in range(geom.GetGeometryCount()):
            poly = geom.GetGeometryRef(pi)
            for ri in range(poly.GetGeometryCount()):
                draw_ring(poly.GetGeometryRef(ri))


def _add_legend(fig, ax, cmap, vmin_raw, vmax_raw, title, scale=1.0):
    ticks = np.linspace(vmin_raw, vmax_raw, LEGEND_TICKS)

    fig.canvas.draw()
    bbox = ax.get_position()
    left = bbox.x0
    bottom = max(0.0, bbox.y0 - 0.045)
    w, h = 0.22, 0.02

    cax = fig.add_axes([left, bottom, w, h])
    sm = plt.cm.ScalarMappable(norm=Normalize(vmin=vmin_raw, vmax=vmax_raw), cmap=cmap)
    sm.set_array([])
    cb = fig.colorbar(sm, cax=cax, orientation="horizontal")
    cb.set_ticks(ticks)
    cb.set_ticklabels([f"{(float(t) * scale):.6g}" for t in ticks])
    cb.outline.set_visible(False)
    cax.set_title(title, fontsize=10, pad=6)
    cax.tick_params(axis="x", labelsize=9, length=0)

    pad = 0.012
    box_w = 0.014

    nd_ax = fig.add_axes([left + w + pad, bottom, box_w, h])
    nd_ax.axis("off")
    nd_ax.add_patch(
        Rectangle(
            (0, 0),
            1,
            1,
            transform=nd_ax.transAxes,
            facecolor=ZERO_COLOR,
            edgecolor="none",
        )
    )
    fig.text(
        left + w + pad + box_w + 0.004,
        bottom + h / 2,
        "NoData",
        va="center",
        ha="left",
        fontsize=9,
    )


def make_screens(
    gas, date_str, parent_cod, rasters_root, vector_path, base_vector_path, out_dir
):

    gas = gas.upper()
    os.makedirs(out_dir, exist_ok=True)

    tif_path = _find_raster(rasters_root, gas, date_str)
    ds = gdal.Open(tif_path)
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray().astype(np.float32)

    zero_mask = arr == 0.0

    valid = np.isfinite(arr) & (~zero_mask)
    if not np.any(valid):
        vmin_clip, vmax_clip = 0.0, 1.0
    else:
        v = arr[valid]
        vmin_clip = float(np.percentile(v, PCLIP_LOW))
        vmax_clip = float(np.percentile(v, PCLIP_HIGH))
        if vmax_clip == vmin_clip:
            vmax_clip = vmin_clip + 1e-12

    norm = np.clip((arr - vmin_clip) / (vmax_clip - vmin_clip), 0.0, 1.0)

    cmap = _spectrum_cmap()
    inv_gt = _inv_gt(ds)
    W, H = ds.RasterXSize, ds.RasterYSize

    ras_srs = osr.SpatialReference()
    wkt = ds.GetProjection()
    ras_srs.ImportFromWkt(wkt) if wkt else ras_srs.ImportFromEPSG(4326)
    ras_srs = _srs_axis(ras_srs)

    unit = GAS_UNITS.get(gas, "unit")
    scale = LEGEND_SCALE.get(gas, 1.0)
    legend_title = f"{gas} Konsentratsiyasi ({unit})"

    zero_layer = np.where(zero_mask, 1, 0).astype(np.uint8)
    zero_cmap = ListedColormap([(0, 0, 0, 0), mcolors.to_rgba(ZERO_COLOR, 1.0)])

    bds = ogr.Open(base_vector_path)
    blyr = bds.GetLayer(0)
    b_srs = _srs_axis(blyr.GetSpatialRef(), fallback_epsg=3857)
    bct = (
        osr.CoordinateTransformation(b_srs, ras_srs)
        if not b_srs.IsSame(ras_srs)
        else None
    )

    def clamp_view(ax, x0, x1, y0, y1):
        xmin = max(0.0, min(x0, x1))
        xmax = min(float(W), max(x0, x1))
        ymin = max(0.0, min(y0, y1))
        ymax = min(float(H), max(y0, y1))
        if xmax <= xmin:
            xmin, xmax = 0.0, float(W)
        if ymax <= ymin:
            ymin, ymax = 0.0, float(H)
        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymax, ymin)

    def render_one(mode, out_png, base_w, highlight):
        fig, ax = plt.subplots(figsize=(10, 8))
        ax.imshow(norm, cmap=cmap, interpolation=DISPLAY_INTERP_MAIN, zorder=0)
        ax.imshow(
            zero_layer, cmap=zero_cmap, interpolation=DISPLAY_INTERP_ZERO, zorder=2
        )

        vds = ogr.Open(vector_path)
        lyr = vds.GetLayer(0)
        vec_srs = _srs_axis(lyr.GetSpatialRef(), fallback_epsg=3857)
        ct = (
            osr.CoordinateTransformation(vec_srs, ras_srs)
            if not vec_srs.IsSame(ras_srs)
            else None
        )

        if mode == "feature":
            feat = _get_feature_by_parent_cod(lyr, parent_cod)
            g_sel = feat.GetGeometryRef().Clone()
            if ct:
                g_sel.Transform(ct)
            xmin, xmax, ymin, ymax = g_sel.GetEnvelope()
        else:
            xmin, xmax, ymin, ymax = _layer_extent_in_raster_srs(lyr, ct)
            g_sel = None

        padx = (xmax - xmin) * ZOOM_PAD
        pady = (ymax - ymin) * ZOOM_PAD
        xmin -= padx
        xmax += padx
        ymin -= pady
        ymax += pady

        x0, y0 = gdal.ApplyGeoTransform(inv_gt, xmin, ymin)
        x1, y1 = gdal.ApplyGeoTransform(inv_gt, xmax, ymax)
        clamp_view(ax, x0, x1, y0, y1)

        blyr.ResetReading()
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
        _add_legend(fig, ax, cmap, vmin_clip, vmax_clip, legend_title, scale)
        fig.savefig(out_png, dpi=200, bbox_inches="tight", pad_inches=0)
        plt.close(fig)

    out_rayon = os.path.join(out_dir, f"{gas}_{date_str}_rayon_screen.png")
    out_mintaqa = os.path.join(out_dir, f"{gas}_{date_str}_mintaqa_screen.png")

    render_one("feature", out_rayon, BASE_W_RAYON, True)
    render_one("layer", out_mintaqa, BASE_W_MINTAQA, False)

    return {"rayon": out_rayon, "mintaqa": out_mintaqa, "raster": tif_path}
