# tests/conftest.py
from __future__ import annotations

import importlib
import os
import sys
from types import SimpleNamespace

import pytest


def reload_module(module_name: str):
    """
    Перезагружает модуль, чтобы он увидел новые env / моки.
    ВАЖНО: сначала удаляем из sys.modules.
    """
    if module_name in sys.modules:
        del sys.modules[module_name]
    return importlib.import_module(module_name)


@pytest.fixture()
def fake_env_and_ee(monkeypatch, tmp_path):
    """
    Делает так, чтобы download_all.py можно было импортировать в CI:
    - подставляет env
    - подменяет ee модуль на фейковый
    """
    # --- ENV ---
    monkeypatch.setenv("PROJECT_ID", "dummy-project")
    monkeypatch.setenv("LEFT_LON", "55.0")
    monkeypatch.setenv("RIGHT_LON", "56.0")
    monkeypatch.setenv("BOTTOM_LAT", "40.0")
    monkeypatch.setenv("TOP_LAT", "41.0")
    monkeypatch.setenv("SAVE_PATH", str(tmp_path))
    monkeypatch.setenv("START_DATE", "2025-01-01")
    monkeypatch.setenv("END_DATE", "2025-01-01")

    monkeypatch.setenv("MAX_RETRIES", "2")
    monkeypatch.setenv("BASE_SLEEP", "0")
    monkeypatch.setenv("MAX_SLEEP", "0")
    monkeypatch.setenv("HTTP_TIMEOUT", "1")
    monkeypatch.setenv("SKIP_DAY_ON_FAIL", "1")

    # --- FAKE ee ---
    class _FakeEE:
        class Geometry:
            @staticmethod
            def Rectangle(_coords):
                return "REGION"

        def Initialize(self, project=None):
            return None

        def ImageCollection(self, *_a, **_k):
            raise RuntimeError("ImageCollection должен быть замокан внутри теста")

    fake_ee = _FakeEE()

    # ВАЖНО: подменяем импортируемый модуль "ee"
    monkeypatch.setitem(sys.modules, "ee", fake_ee)

    return tmp_path
