# tests/conftest.py
from __future__ import annotations

import importlib
import sys


def reload_module(module_name: str):
    """
    Перезагрузка модуля (если вдруг понадобится).
    """
    if module_name in sys.modules:
        del sys.modules[module_name]
    return importlib.import_module(module_name)
