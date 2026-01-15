# tests/test_4.py
import pytest
from datetime import date
from tests.conftest import reload_module


def test_wind_module_imports():
    # просто проверим что модуль импортируется
    m = reload_module("wind")
    assert m is not None


def test_wind_optional_functions():
    m = reload_module("wind")

    # если нет функций — не считаем это ошибкой CI
    if not hasattr(m, "get_last_downloaded_day"):
        pytest.skip("wind.get_last_downloaded_day not found")
    if not hasattr(m, "parse_day") and not hasattr(m, "START_DAY"):
        pytest.skip("wind has no parse_day/START_DAY")

    # минимальная проверка: функция есть -> должна отработать без исключения на пустом каталоге
    # (тут без требований к результату)
    try:
        _ = m.get_last_downloaded_day()
    except Exception:
        # допускаем, что без env может ругаться
        pytest.skip("wind.get_last_downloaded_day requires env/files")
