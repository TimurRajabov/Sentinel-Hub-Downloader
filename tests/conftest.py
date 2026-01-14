import importlib
import sys
import types

import pytest


class _FakeEEImage:
    def __init__(self, bands=None):
        self._bands = bands or []

    def bandNames(self):
        return self

    def getInfo(self):
        return self._bands

    def toBands(self):
        return self

    def select(self, *_args, **_kwargs):
        return self

    def rename(self, *_args, **_kwargs):
        return self

    def clip(self, *_args, **_kwargs):
        return self

    def mean(self):
        return self

    def getDownloadURL(self, _params):
        return "http://example.com/fake.tif"


class _FakeEECollection:
    def __init__(self, bands=None, size=48):
        self._bands = bands or []
        self._size = size

    def filterDate(self, *_args, **_kwargs):
        return self

    def filterBounds(self, *_args, **_kwargs):
        return self

    def select(self, *_args, **_kwargs):
        return self

    def sort(self, *_args, **_kwargs):
        return self

    def size(self):
        return self

    def getInfo(self):
        return self._size

    def toBands(self):
        return _FakeEEImage(bands=self._bands)


@pytest.fixture
def fake_env_and_ee(monkeypatch, tmp_path):
    # ENV, чтобы импорт не падал
    monkeypatch.setenv("PROJECT_ID", "test-project")
    monkeypatch.setenv("LEFT_LON", "0")
    monkeypatch.setenv("RIGHT_LON", "1")
    monkeypatch.setenv("BOTTOM_LAT", "0")
    monkeypatch.setenv("TOP_LAT", "1")
    monkeypatch.setenv("SAVE_PATH", str(tmp_path))
    monkeypatch.setenv("START_DATE", "2025-01-01")
    monkeypatch.setenv("END_DATE", "2025-01-02")

    # Фейковый ee модуль
    fake_ee = types.SimpleNamespace()

    def _Initialize(*_args, **_kwargs):
        return None

    class _Geometry:
        @staticmethod
        def Rectangle(_coords):
            return {"type": "Rectangle", "coords": _coords}

    def _ImageCollection(_collection_id):
        # Сгенерим 48 каналов: U0,V0,U1,V1,...,U23,V23
        bands = []
        for h in range(24):
            bands.append(f"u_component_of_wind_10m_{h}")
            bands.append(f"v_component_of_wind_10m_{h}")
        return _FakeEECollection(bands=bands, size=48)

    def _Date(_iso):
        return types.SimpleNamespace(advance=lambda *_a, **_k: None)

    fake_ee.Initialize = _Initialize
    fake_ee.Geometry = _Geometry
    fake_ee.ImageCollection = _ImageCollection
    fake_ee.Date = _Date
    fake_ee.Image = _FakeEEImage

    # Вставляем в sys.modules, чтобы import ee взял фейк
    sys.modules["ee"] = fake_ee

    return tmp_path


def reload_module(name: str):
    """Удобно: импорт/перезагрузка после выставления env и sys.modules['ee']"""
    if name in sys.modules:
        del sys.modules[name]
    return importlib.import_module(name)
