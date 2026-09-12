import importlib.util
import sys
import types
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).parents[1] / "plugins.v3" / "lunatvsource"
PACKAGE_NAME = "app.plugins.lunatvsource"


def _ensure_host_package() -> None:
    """Create only the parent namespace needed by standalone plugin tests."""
    try:
        app_package = __import__("app", fromlist=["plugins"])
    except ModuleNotFoundError:
        app_package = types.ModuleType("app")
        app_package.__path__ = []
        sys.modules["app"] = app_package

    try:
        plugins_package = __import__("app.plugins", fromlist=["lunatvsource"])
    except ModuleNotFoundError:
        plugins_package = types.ModuleType("app.plugins")
        plugins_package.__path__ = [str(PLUGIN_DIR.parent)]
        sys.modules["app.plugins"] = plugins_package
        setattr(app_package, "plugins", plugins_package)


if PACKAGE_NAME not in sys.modules:
    _ensure_host_package()
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    setattr(sys.modules["app.plugins"], "lunatvsource", module)
    spec.loader.exec_module(module)


@pytest.fixture(autouse=True)
def disable_live_stream_probe(monkeypatch):
    """Unit tests must never contact the public CMS video endpoints."""

    plugin_module = sys.modules[PACKAGE_NAME]
    monkeypatch.setattr(plugin_module, "probe_stream_height", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(plugin_module, "_HostMediaSource", None)
