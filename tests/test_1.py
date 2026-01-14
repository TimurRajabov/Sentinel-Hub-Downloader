import json
from unittest.mock import MagicMock, patch

import numpy as np
import pytest # type: ignore
from fastapi.testclient import TestClient

from api_all import GAS_UNITS, app, compute_mean_for_gas

client = TestClient(app)


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
# МОК корректного xarray объекта
# -----------------------------------------
class MockDataArray:
    def __init__(self, value):
        self._array = np.array([[value, value], [value, value]])

    def squeeze(self):
        return self

    class rio:
        @staticmethod
        def write_crs(*args, **kwargs):
            return MockDataArray(MockDataArray.value)

        @staticmethod
        def clip(*args, **kwargs):
            return MockDataArray.array

    @property
    def values(self):
        return self._array


def mock_rasterio_array(value=0.002):
    obj = MagicMock()

    # xarray-style API
    obj.squeeze.return_value = obj
    obj.rio.write_crs.return_value = obj

    # Возвращаем объект, который ИМЕЕТ .values
    arr = np.array([[value, value], [value, value]])
    obj.rio.clip.return_value = MagicMock(values=arr)

    return obj


# -----------------------------------------
# Тесты compute_mean_for_gas
# -----------------------------------------
def test_compute_mean_for_ch4():
    arr = np.array([2000, 3000, 4000])
    assert compute_mean_for_gas("CH4", arr) == 3.0


def test_compute_mean_for_o3():
    arr = np.array([0.1, 0.2, 0.3])
    assert compute_mean_for_gas("O3", arr) == round(np.mean(arr), 3)


# -----------------------------------------
# API тест для всех газов
# -----------------------------------------
@pytest.mark.parametrize("gas", ["CH4", "CO", "NO2", "SO2", "HCHO", "O3", "AERAI"])
@patch("api_all.os.path.exists", return_value=True)
@patch("api_all.list_tiffs", return_value=["data/new_tif/X/X_2025-11-20.tif"])
@patch("api_all.rioxarray.open_rasterio", return_value=mock_rasterio_array(0.005))
@patch("builtins.open")
def test_air_monitoring_points(mock_open, mock_rio, mock_tiffs, mock_exists, gas):

    # Нормальный mock для open(...)
    mock_open.return_value.__enter__.return_value.read.return_value = json.dumps(
        MOCK_GEOJSON
    )

    response = client.get(f"/api/air_monitoring_points?gas={gas}&region=1726")

    assert response.status_code == 200

    data = response.json()[0]

    assert data["gas"] == gas
    assert data["unit"] == GAS_UNITS[gas]
    assert "period" in data
    assert isinstance(data["mean"], float)
    assert data["mean"] >= 0
