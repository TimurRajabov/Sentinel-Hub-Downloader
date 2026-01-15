from __future__ import annotations

from datetime import date
from pathlib import Path

from tests.conftest import reload_module


def _set_outdir(m, tmp_path: Path) -> Path:
    """
    Принудительно направляем temp.py писать в tmp_path/temp (или читать оттуда),
    независимо от того, как в модуле назван путь (SAVE_PATH/OUT_DIR/OUTPUT_DIR).
    """
    out = tmp_path / "temp"
    out.mkdir(parents=True, exist_ok=True)

    # разные проекты называют по-разному — перестрахуемся
    for name in ("SAVE_PATH", "OUT_DIR", "OUTPUT_DIR", "OUTPUT_PATH", "TEMP_OUT_DIR"):
        if hasattr(m, name):
            setattr(m, name, str(out))

    # если модуль делает os.path.join(SAVE_PATH, "temp") — нужно, чтобы SAVE_PATH был tmp_path
    if hasattr(m, "SAVE_PATH"):
        setattr(m, "SAVE_PATH", str(tmp_path))

    return out


def test_parse_day(fake_env_and_ee):
    m = reload_module("temp")
    assert m.parse_day("2025-01-01") == date(2025, 1, 1)
    assert m.parse_day(None) is None


def test_get_last_downloaded_day(fake_env_and_ee, tmp_path):
    m = reload_module("temp")
    out = _set_outdir(m, tmp_path)

    # ВАЖНО: файлы создаём именно там, где temp.py реально читает
    (out / "20250105_U_00.tif").write_bytes(b"x")
    (out / "20250102_V_01.tif").write_bytes(b"x")
    (out / "badname.tif").write_bytes(b"x")

    assert m.get_last_downloaded_day() == date(2025, 1, 5)


def test_run_sync_no_files_branch(fake_env_and_ee, tmp_path, monkeypatch):
    """
    Ветка: last_day = None -> стартуем с START_DAY -> скачиваем дни.
    """
    m = reload_module("temp")
    _set_outdir(m, tmp_path)

    m.START_DAY = date(2025, 1, 1)
    m.END_DAY = date(2025, 1, 2)

    # last_day = None
    monkeypatch.setattr(m, "get_last_downloaded_day", lambda: None)

    # не ходим в EE/requests
    calls = {"n": 0, "days": []}

    def fake_download(day):
        calls["n"] += 1
        calls["days"].append(day)

    monkeypatch.setattr(m, "download_era5_for_day", fake_download)

    res = m.run_sync()

    # run_sync может быть без return
    assert res is None or res is False or res is True
    assert calls["n"] == 2
    assert calls["days"] == [date(2025, 1, 1), date(2025, 1, 2)]


def test_run_sync_continue_from_last_day(fake_env_and_ee, tmp_path, monkeypatch):
    """
    Ветка: last_day есть -> начинаем с last_day+1.
    """
    m = reload_module("temp")
    _set_outdir(m, tmp_path)

    m.START_DAY = date(2025, 1, 1)
    m.END_DAY = date(2025, 1, 3)

    monkeypatch.setattr(m, "get_last_downloaded_day", lambda: date(2025, 1, 1))

    called_days = []

    def fake_download(day):
        called_days.append(day)

    monkeypatch.setattr(m, "download_era5_for_day", fake_download)

    res = m.run_sync()
    assert res is None or res is False or res is True

    # должны скачаться 2025-01-02 и 2025-01-03
    assert called_days == [date(2025, 1, 2), date(2025, 1, 3)]


def test_run_sync_already_done_branch(fake_env_and_ee, tmp_path, monkeypatch):
    """
    Ветка: day > END_DAY -> 'уже всё скачано' и выход.
    """
    m = reload_module("temp")
    _set_outdir(m, tmp_path)

    m.START_DAY = date(2025, 1, 1)
    m.END_DAY = date(2025, 1, 1)

    # last_day == END_DAY -> day = last_day+1 = 2025-01-02 > END_DAY
    monkeypatch.setattr(m, "get_last_downloaded_day", lambda: date(2025, 1, 1))

    # если случайно вызовется — тест должен упасть
    monkeypatch.setattr(
        m,
        "download_era5_for_day",
        lambda _d: (_ for _ in ()).throw(
            AssertionError("download_era5_for_day should NOT be called")
        ),
    )

    res = m.run_sync()
    assert res is None or res is False or res is True
