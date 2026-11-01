import importlib
import sys
from pathlib import Path
from types import ModuleType


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, package: str = "_checkin_test_plugin") -> ModuleType:
    test_package = sys.modules.get(package)
    if test_package is None:
        test_package = ModuleType(package)
        test_package.__package__ = package
        test_package.__path__ = [str(_REPOSITORY_ROOT)]
        sys.modules[package] = test_package

    return importlib.import_module(f"{package}.{name}")
