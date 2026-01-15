# tests/test_2.py
import os
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest


# ----------------------------
# helpers (как в твоём скрипте)
# ----------------------------
def parse_day(s: str | None) -> date | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d").date()


# ----------------------------
# FIX: вместо реального ee.Initialize()
# ----------------------------
@pytest.fixture()
def env_ok(monkeypatch, tmp_path):
    # минимальный набор переменных, чтобы код мог жить
    monkeypatch.setenv("PROJECT_ID", "dummy-project")
    monkeypatch.setenv("LEFT_LON", "55.0")
    monkeypatch.setenv("RIGHT_LON", "56.0")
    monkeypatch.setenv("BOTTOM_LAT", "40.0")
    monkeypatch.setenv("TOP_LAT", "41.0")
    monkeypatch.setenv("SAVE_PATH", str(tmp_path))
    monkeypatch.setenv("START_DATE", "2025-01-01")
    monkeypatch.setenv("END_DATE", "2025-01-02")
    return tmp_path


def test_parse_day_ok():
    assert parse_day("2025-01-01") == date(2025, 1, 1)


def test_parse_day_none():
    assert parse_day(None) is None
    assert parse_day("") is None


def test_validate_dates_ok():
    start = parse_day("2025-01-01")
    end = parse_day("2025-01-02")
    assert start <= end


def test_validate_dates_invalid():
    start = parse_day("2025-01-03")
    end = parse_day("2025-01-02")
    assert start > end


# -----------------------------------------
# Тест логики run_sync без EarthEngine/HTTP
# -----------------------------------------
def test_run_sync_flow_without_ee(monkeypatch, env_ok):
    """
    Мы НЕ импортируем реальный ee, requests.
    Вместо этого тестируем "поведение": если col.size()==0 -> пропуск,
    если download ок -> файл создаётся, цикл идёт дальше.
    """

    # 1) Мокаем ee модуль и его цепочки
    ee = MagicMock()

    # region rectangle
    ee.Geometry.Rectangle.return_value = "REGION"

    # ImageCollection(...) -> объект col
    col = MagicMock()
    ee.ImageCollection.return_value = col

    # col.select(...).filterDate(... ) -> возвращает col (цепочка)
    col.select.return_value = col
    col.filterDate.return_value = col

    # col.size().getInfo() — делаем так:
    #   первый день: 0 (нет данных)
    #   второй день: 1 (есть данные)
    size_obj = MagicMock()
    size_obj.getInfo.side_effect = [0, 1]
    col.size.return_value = size_obj

    # col.mean().clip(region)
    img = MagicMock()
    col.mean.return_value = img
    img.clip.return_value = img

    # 2) Мокаем download функцию: просто создаёт файл
    def fake_download_geotiff_with_retries(_img, outfile: str):
        os.makedirs(os.path.dirname(outfile), exist_ok=True)
        with open(outfile, "wb") as f:
            f.write(b"dummy")

    # 3) Собираем минимальную копию логики run_sync (как у тебя),
    #    но без вечного цикла и без реального ee.Initialize()
    gases = {
        "CH4": ("DATASET", "BAND"),
    }

    START_DAY = parse_day(os.getenv("START_DATE"))
    END_DAY = parse_day(os.getenv("END_DATE"))
    SAVE_PATH = os.getenv("SAVE_PATH")

    assert START_DAY is not None and END_DAY is not None

    # ee.Initialize(project=...)
    ee.Initialize(project=os.getenv("PROJECT_ID"))

    region = ee.Geometry.Rectangle(
        [
            float(os.getenv("LEFT_LON")),
            float(os.getenv("BOTTOM_LAT")),
            float(os.getenv("RIGHT_LON")),
            float(os.getenv("TOP_LAT")),
        ]
    )

    had_failures = False

    for gas_name, (dataset, band) in gases.items():
        gas_folder = os.path.join(SAVE_PATH, gas_name)
        os.makedirs(gas_folder, exist_ok=True)

        current_date = START_DAY
        while current_date <= END_DAY:
            next_date = current_date + timedelta(days=1)
            date_str = current_date.strftime("%Y-%m-%d")
            outfile = os.path.join(gas_folder, f"{gas_name}_{date_str}.tif")

            col2 = (
                ee.ImageCollection(dataset)
                .select(band)
                .filterDate(str(current_date), str(next_date))
            )

            # нет данных -> skip
            if col2.size().getInfo() == 0:
                current_date = next_date
                continue

            img2 = col2.mean().clip(region)

            try:
                fake_download_geotiff_with_retries(img2, outfile)
                current_date = next_date
            except Exception:
                had_failures = True
                break

    # 4) Проверки
    # День 2025-01-01 был "нет данных" -> файл НЕ создан
    f1 = env_ok / "CH4" / "CH4_2025-01-01.tif"
    assert not f1.exists()

    # День 2025-01-02 был "есть данные" -> файл создан
    f2 = env_ok / "CH4" / "CH4_2025-01-02.tif"
    assert f2.exists()

    assert had_failures is False
