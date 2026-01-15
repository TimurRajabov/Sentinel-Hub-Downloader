# tests/test_1.py
import json
import sys
import types
from unittest.mock import MagicMock

import numpy as np
import pytest
from fastapi.testclient import TestClient


# -----------------------------------------
# FIX: не даём import-цепочке упасть (osgeo / word_grafik / make_word)
# -----------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _stub_heavy_modules():
    # 1) stub osgeo
    osgeo = types.ModuleType("osgeo")
    osgeo.gdal = types.SimpleNamespace(UseExceptions=lambda: None)
    osgeo.ogr = types.SimpleNamespace(UseExceptions=lambda: None)
    osgeo.osr = types.SimpleNamespace(UseExceptions=lambda: None)
    sys.modules.setdefault("osgeo", osgeo)

    # 2) stub word_grafik.make_grafik
    word_grafik = types.ModuleType("word_grafik")
    word_grafik.make_grafik = lambda *args, **kwargs: {
        "png": "dummy.png",
        "region_name": "dummy",
    }
    sys.modules.setdefault("word_grafik", word_grafik)

    # 3) stub make_word.build_docx
    make_word = types.ModuleType("make_word")
    make_word.build_docx = lambda *args, **kwargs: "dummy.docx"
    sys.modules.setdefault("make_word", make_word)


# -----------------------------------------
# MOCK GEOJSON
# -----------------------------------------
MOCK_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"region_soato": 1726},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [55.0, 40.0],
                        [56.0, 40.0],
                        [56.0, 41.0],
                        [55.0, 41.0],
                        [55.0, 40.0],
                    ]
                ],
            },
        }
    ],
}


# -----------------------------------------
# Мок xarray DataArray, который использует твой api_all:
# da = rioxarray.open_rasterio(...).squeeze()
# da.rio.write_crs(...)
# da.rio.clip(...)
# clipped.values.flatten()
# -----------------------------------------
def mock_rioxarray_dataarray(value: float = 0.005):
    da = MagicMock()
    da.squeeze.return_value = da

    # чтобы da.rio.crs существовал
    da.rio.crs = MagicMock()
    da.rio.write_crs.return_value = da

    arr = np.array([[value, value], [value, value]], dtype=float)
    da.rio.clip.return_value = MagicMock(values=arr)

    return da


@pytest.fixture()
def api(monkeypatch):
    """
    Импортируем api_all ТОЛЬКО после того как:
    - модули застаблены
    - env выставлен (чтобы не было RuntimeError: SAVE_PATH is not set)
    """
    monkeypatch.setenv("SAVE_PATH", "data")  # чтобы api_all не падал
    monkeypatch.setenv("ENV", "test")

    import api_all  # noqa: E402

    return api_all


@pytest.fixture()
def client(api):
    return TestClient(api.app)


# -----------------------------------------
# Тесты compute_mean_for_gas
# -----------------------------------------
def test_compute_mean_for_ch4(api):
    arr = np.array([2000, 3000, 4000])
    assert api.compute_mean_for_gas("CH4", arr) == 3.0


def test_compute_mean_for_o3(api):
    arr = np.array([0.1, 0.2, 0.3])
    assert api.compute_mean_for_gas("O3", arr) == round(np.mean(arr), 3)


# -----------------------------------------
# API тест для всех газов (без GDAL)
# -----------------------------------------
@pytest.mark.parametrize("gas", ["CH4", "CO", "NO2", "SO2", "HCHO", "O3", "AERAI"])
def test_air_monitoring_points(monkeypatch, api, client, gas):
    # 1) подменяем чтение geojson
    class _Ctx:
        def __enter__(self):
            m = MagicMock()
            m.read.return_value = json.dumps(MOCK_GEOJSON)
            return m

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: _Ctx())

    # 2) делаем вид что tif существует
    monkeypatch.setattr(api.os.path, "exists", lambda p: True)

    # 3) list_tiffs возвращает "какой-то" tif
    monkeypatch.setattr(
        api, "list_tiffs", lambda *args, **kwargs: ["data/new_tif/X/X_2025-11-20.tif"]
    )

    # 4) rioxarray.open_rasterio -> возвращает мок da
    monkeypatch.setattr(
        api.rioxarray,
        "open_rasterio",
        lambda *args, **kwargs: mock_rioxarray_dataarray(0.005),
    )

    response = client.get(f"/api/air_monitoring_points?gas={gas}&region=1726")
    assert response.status_code == 200

    data = response.json()[0]
    assert data["gas"] == gas
    assert data["unit"] == api.GAS_UNITS[gas]
    assert "period" in data
    assert isinstance(data["mean"], (float, int))
    assert float(data["mean"]) >= 0.0
