import os
import glob
import json
import csv
import numpy as np
import rasterio
from rasterio.mask import mask
from rasterio.warp import transform_geom

GEOJSON_PATH = "/home/temur/Documents/Work/goo/geojson/uzbekistan_regions_backup.geojson"

# Папка с TIF по газу
DATA_DIR = "/home/temur/Documents/Work/goo/data/CH4"
GAS_NAME = "CH4"

REGION_ID_FIELD = "region_soato"
REGION_NAME_FIELD = "name"

# Если % нулей в вырезке >= порога -> считаем, что данных нет (fill)
ZERO_NODATA_THRESHOLD_PCT = 99.9

# Для каких газов считать 0 как fill-value (обычно для CH4 это верно)
DROP_ZERO_FOR_GASES = {"CH4", "CO", "NO2", "SO2", "HCHO", "O3", "AERAI"}


def safe_region_id(props: dict) -> str:
    v = props.get(REGION_ID_FIELD)
    return str(v) if v is not None else "unknown"


def safe_region_name(props: dict) -> str:
    v = props.get(REGION_NAME_FIELD)
    return str(v) if v is not None else ""


def extract_period_from_filename(fname: str) -> str:
    base = os.path.basename(fname).replace(".tif", "")
    return base.split("_")[-1]


def read_regions(geojson_path: str):
    with open(geojson_path, "r", encoding="utf-8") as f:
        gj = json.load(f)
    feats = gj.get("features", [])
    if not feats:
        raise RuntimeError("❌ GeoJSON не содержит features")
    return feats


def get_tifs(data_dir: str):
    tifs = sorted(glob.glob(os.path.join(data_dir, "*.tif")))
    if not tifs:
        raise RuntimeError(f"❌ В папке нет .tif: {data_dir}")
    return tifs


def compute_stats(values_1d: np.ndarray, gas: str) -> dict | None:
    v = values_1d[np.isfinite(values_1d)]
    v = v[(v >= 0) & (v < 1e20)]

    if v.size == 0:
        return None

    zero_pct = float((v == 0).mean() * 100.0)

    # Если почти всё нули — это не данные, а заполнение (fill)
    if zero_pct >= ZERO_NODATA_THRESHOLD_PCT:
        return None

    # Для большинства газов 0 — это fill, поэтому выкидываем
    if gas.upper() in DROP_ZERO_FOR_GASES:
        v2 = v[v != 0]
        if v2.size == 0:
            return None
        v = v2

    return {
        "count": int(v.size),
        "mean": float(v.mean()),
        "min": float(v.min()),
        "max": float(v.max()),
        "median": float(np.median(v)),
        "std": float(v.std()),
        "zero_pct": zero_pct,  # % нулей ДО выкидывания, полезно для диагностики
    }


def mask_values_for_feature(src, feature_geom) -> np.ndarray:
    """
    Вырезаем растр по полигону (crop=True), геометрию конвертим из EPSG:4326 -> CRS растра.
    """
    if src.crs is None:
        raise RuntimeError("❌ У TIFF нет CRS (src.crs == None).")

    geom_in_src_crs = transform_geom("EPSG:4326", src.crs, feature_geom, precision=6)

    out_image, _ = mask(src, [geom_in_src_crs], crop=True)
    band1 = out_image[0]

    # если mask() вернул masked array, то compressed() отдаст только валидные
    if hasattr(band1, "compressed"):
        values = band1.compressed()
    else:
        values = np.asarray(band1).ravel()

    # если в src есть nodata — убираем
    if src.nodata is not None:
        values = values[values != src.nodata]

    return values


def main():
    regions = read_regions(GEOJSON_PATH)
    tifs = get_tifs(DATA_DIR)

    results = []

    for tif_path in tifs:
        period = extract_period_from_filename(tif_path)
        print(f"\n====================\n📄 TIFF: {os.path.basename(tif_path)} | period={period}\n====================")

        with rasterio.open(tif_path) as src:
            print(f"CRS: {src.crs} | nodata: {src.nodata} | size: {src.width}x{src.height}")

            for feat in regions:
                props = feat.get("properties", {}) or {}
                geom = feat.get("geometry")
                if not geom:
                    continue

                region_id = safe_region_id(props)
                region_name = safe_region_name(props)

                try:
                    values = mask_values_for_feature(src, geom)
                    stats = compute_stats(values, GAS_NAME)
                except Exception as e:
                    print(f"⚠️ region={region_id} {region_name} -> error: {e}")
                    row = {
                        "gas": GAS_NAME,
                        "period": period,
                        "region_id": region_id,
                        "region_name": region_name,
                        "status": "ERROR",
                        "error": str(e),
                    }
                    results.append(row)
                    continue

                if stats is None:
                    print(f"⚠️ region={region_id} {region_name} -> NO_DATA (пусто/нули)")
                    row = {
                        "gas": GAS_NAME,
                        "period": period,
                        "region_id": region_id,
                        "region_name": region_name,
                        "status": "NO_DATA",
                        "count": 0,
                        "mean": 0.0,
                        "min": 0.0,
                        "max": 0.0,
                        "median": 0.0,
                        "std": 0.0,
                        "zero_pct": 100.0,
                    }
                    results.append(row)
                    continue

                row = {
                    "gas": GAS_NAME,
                    "period": period,
                    "region_id": region_id,
                    "region_name": region_name,
                    "status": "OK",
                    **stats,
                }
                results.append(row)

                print(
                    f"🧭 region={region_id} {region_name} | "
                    f"mean={row['mean']:.6f} min={row['min']:.6f} max={row['max']:.6f} "
                    f"median={row['median']:.6f} std={row['std']:.6f} zeros%={row['zero_pct']:.2f} "
                    f"(n={row['count']})"
                )

    out_csv = os.path.join(os.path.dirname(DATA_DIR), f"{GAS_NAME}_regions_stats.csv")

    if results:
        # соберём все возможные колонки (потому что есть ERROR с error)
        all_keys = set()
        for r in results:
            all_keys.update(r.keys())
        keys = [
            "gas", "period", "region_id", "region_name", "status",
            "count", "mean", "min", "max", "median", "std", "zero_pct", "error"
        ]
        # добавим те, что вдруг появятся
        for k in sorted(all_keys):
            if k not in keys:
                keys.append(k)

        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(results)

        print(f"\n✅ CSV сохранён: {out_csv}")
    else:
        print("\n⚠️ Результатов нет")


if __name__ == "__main__":
    main()
