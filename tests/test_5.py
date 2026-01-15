import importlib
import json
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

# -----------------------------------------
# MOCK GEOJSON (будем подсовывать через open)
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


def mock_rasterio_array(value=0.005):
    obj = MagicMock()
    obj.squeeze.return_value = obj
    obj.rio.write_crs.return_value = obj
    arr = np.array([[value, value], [value, value]])
    obj.rio.clip.return_value = MagicMock(values=arr)
    return obj


@pytest.fixture()
def api(monkeypatch, tmp_path):
    """
    Импортируем api_all только ПОСЛЕ выставления env,
    чтобы не было RuntimeError по SAVE_PATH/GEOJSON_PATH.
    """
    # чтобы api_all не падал на старте:
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("SAVE_PATH", str(tmp_path / "data"))
    monkeypatch.setenv("OUTPUT_ROOT", str(tmp_path / "output"))
    monkeypatch.setenv("GEOJSON_PATH", str(tmp_path / "geo.json"))
    monkeypatch.setenv("GEOJSON_PATH_1", str(tmp_path / "geo1.json"))

    # на всякий случай создадим фейковые файлы, если код реально попытается читать
    (tmp_path / "geo.json").write_text(json.dumps(MOCK_GEOJSON), encoding="utf-8")
    (tmp_path / "geo1.json").write_text(json.dumps(MOCK_GEOJSON), encoding="utf-8")

    import api_all  # noqa

    importlib.reload(api_all)
    return api_all


@pytest.fixture()
def client(api):
    return TestClient(api.app)


def test_compute_mean_for_ch4(api):
    arr = np.array([2000, 3000, 4000])
    assert api.compute_mean_for_gas("CH4", arr) == 3.0


def test_compute_mean_for_o3(api):
    arr = np.array([0.1, 0.2, 0.3])
    assert api.compute_mean_for_gas("O3", arr) == round(np.mean(arr), 3)


@pytest.mark.parametrize("gas", ["CH4", "CO", "NO2", "SO2", "HCHO", "O3", "AERAI"])
@patch("api_all.os.path.exists", return_value=True)
@patch("api_all.list_tiffs", return_value=["data/new_tif/X/X_2025-11-20.tif"])
@patch("api_all.rioxarray.open_rasterio", return_value=mock_rasterio_array(0.005))
@patch("builtins.open")
def test_air_monitoring_points(
    mock_open, mock_rio, mock_tiffs, mock_exists, api, client, gas
):
    # мок open(...) чтобы geojson читался из памяти (на случай если api_all делает open(GEOJSON_PATH))
    mock_open.return_value.__enter__.return_value.read.return_value = json.dumps(
        MOCK_GEOJSON
    )

    resp = client.get(f"/api/air_monitoring_points?gas={gas}&region=1726")
    assert resp.status_code == 200

    data = resp.json()[0]
    assert data["gas"] == gas
    assert data["unit"] == api.GAS_UNITS[gas]
    assert "period" in data
    assert isinstance(data["mean"], float)
    assert data["mean"] >= 0
