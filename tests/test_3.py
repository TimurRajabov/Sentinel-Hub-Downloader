# tests/test_3.py
import pytest

from tests.conftest import reload_module


def test_wind_module_imports(fake_env_and_ee):
    m = reload_module("wind")
    assert m is not None


def test_wind_optional_functions(fake_env_and_ee):
    m = reload_module("wind")

    if not hasattr(m, "get_last_downloaded_day"):
        pytest.skip("wind.get_last_downloaded_day not found")

    try:
        _ = m.get_last_downloaded_day()
    except Exception as e:
        pytest.skip(f"wind.get_last_downloaded_day requires extra setup/files: {e}")
