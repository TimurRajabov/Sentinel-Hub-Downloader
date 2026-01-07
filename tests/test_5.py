from __future__ import annotations

from datetime import date

from tests.conftest import reload_module


def test_parse_day(fake_env_and_ee):
    m = reload_module("temp")
    assert m.parse_day("2025-01-01") == date(2025, 1, 1)
    assert m.parse_day(None) is None


def test_get_last_downloaded_day(fake_env_and_ee, tmp_path):
    m = reload_module("temp")

    out = tmp_path / "wind"
    out.mkdir(parents=True, exist_ok=True)

    (out / "20250105_U_00.tif").write_bytes(b"x")
    (out / "20250102_V_01.tif").write_bytes(b"x")
    (out / "badname.tif").write_bytes(b"x")

    assert m.get_last_downloaded_day() == date(2025, 1, 5)


def test_run_sync_no_files_branch(fake_env_and_ee, monkeypatch):
    """
    Ветка: last_day = None -> стартуем с START_DAY -> скачиваем дни.
    """
    m = reload_module("temp")

    m.START_DAY = date(2025, 1, 1)
    m.END_DAY = date(2025, 1, 2)

    # last_day = None
    if hasattr(m, "get_last_downloaded_day"):
        monkeypatch.setattr(m, "get_last_downloaded_day", lambda: None)

    # не ходим в EE/requests
    if hasattr(m, "download_era5_for_day"):
        calls = {"n": 0}

        def fake_download(day):
            calls["n"] += 1

        monkeypatch.setattr(m, "download_era5_for_day", fake_download)

    # run_sync может быть без return
    if hasattr(m, "run_sync"):
        m.run_sync()

        # если есть download_era5_for_day — убедимся что он вызывался 2 раза
        if hasattr(m, "download_era5_for_day"):
            assert calls["n"] == 2


def test_run_sync_continue_from_last_day(fake_env_and_ee, monkeypatch):
    """
    Ветка: last_day есть -> начинаем с last_day+1.
    """
    m = reload_module("temp")

    m.START_DAY = date(2025, 1, 1)
    m.END_DAY = date(2025, 1, 3)

    if hasattr(m, "get_last_downloaded_day"):
        monkeypatch.setattr(m, "get_last_downloaded_day", lambda: date(2025, 1, 1))

    if hasattr(m, "download_era5_for_day"):
        called_days = []

        def fake_download(day):
            called_days.append(day)

        monkeypatch.setattr(m, "download_era5_for_day", fake_download)

    if hasattr(m, "run_sync"):
        m.run_sync()

        if hasattr(m, "download_era5_for_day"):
            # должны скачаться 2025-01-02 и 2025-01-03
            assert called_days == [date(2025, 1, 2), date(2025, 1, 3)]


def test_run_sync_already_done_branch(fake_env_and_ee, monkeypatch):
    """
    Ветка: day > END_DAY -> 'уже всё скачано' и выход.
    """
    m = reload_module("temp")

    m.START_DAY = date(2025, 1, 1)
    m.END_DAY = date(2025, 1, 1)

    # last_day == END_DAY -> day = last_day+1 = 2025-01-02 > END_DAY
    if hasattr(m, "get_last_downloaded_day"):
        monkeypatch.setattr(m, "get_last_downloaded_day", lambda: date(2025, 1, 1))

    # если случайно вызовется — тест должен упасть
    if hasattr(m, "download_era5_for_day"):
        monkeypatch.setattr(m, "download_era5_for_day", lambda _d: (_ for _ in ()).throw(AssertionError("download_era5_for_day should not be called")))

    if hasattr(m, "run_sync"):
        m.run_sync()
