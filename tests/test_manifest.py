import json
from pathlib import Path
from urllib.parse import urlparse

from lunatvsource_test import LunaTVSource


def test_manifest_and_plugin_icons_use_https_url():
    project_root = Path(__file__).resolve().parents[1]
    package = json.loads((project_root / "package.v3.json").read_text(encoding="utf-8"))

    manifest_icon = package["LunaTVSource"]["icon"]
    plugin_icon = LunaTVSource.plugin_icon
    parsed_icon = urlparse(manifest_icon)

    assert manifest_icon == plugin_icon
    assert parsed_icon.scheme == "https"
    assert Path(parsed_icon.path).name == "lunatvsource.png"
    assert (project_root / "icons" / "lunatvsource.png").is_file()
