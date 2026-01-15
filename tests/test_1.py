# tests/test_1.py
import numpy as np


def compute_mean_for_gas_local(gas: str, values: np.ndarray) -> float:
    """
    Локальная "чистая" логика как у тебя обычно в проектах:
    - CH4: /1000
    - NO2/SO2/HCHO: *1e6
    - else: mean
    """
    gas = gas.upper()
    v = values[np.isfinite(values)]
    if v.size == 0:
        return 0.0

    raw = float(np.mean(v))

    if gas == "CH4":
        return round(raw / 1000.0, 3)

    if gas in ("NO2", "SO2", "HCHO"):
        return round(raw * 1e6, 3)

    return round(raw, 3)


def test_compute_mean_for_ch4():
    arr = np.array([2000, 3000, 4000], dtype=float)
    assert compute_mean_for_gas_local("CH4", arr) == 3.0


def test_compute_mean_for_o3():
    arr = np.array([0.1, 0.2, 0.3], dtype=float)
    assert compute_mean_for_gas_local("O3", arr) == round(float(np.mean(arr)), 3)


def test_compute_mean_random():
    rng = np.random.default_rng(42)
    arr = rng.random(1000).astype(float)
    assert compute_mean_for_gas_local("CO", arr) == round(float(np.mean(arr)), 3)


def test_nan_inf_filtered():
    arr = np.array([1.0, np.nan, 3.0, np.inf, -np.inf])
    assert compute_mean_for_gas_local("CO", arr) == round(float(np.mean([1.0, 3.0])), 3)


def test_all_invalid_returns_zero():
    arr = np.array([np.nan, np.inf, -np.inf])
    assert compute_mean_for_gas_local("CO", arr) == 0.0
