import importlib.util
import sys
from pathlib import Path


PLUGIN_DIR = Path(__file__).parents[1] / "plugins.v3" / "lunatvsource"
PACKAGE_NAME = "lunatvsource_test"

if PACKAGE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)

