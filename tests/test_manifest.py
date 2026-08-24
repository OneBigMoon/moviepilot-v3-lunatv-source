import json
from pathlib import Path


def test_marketplace_icon_uses_basename_and_exists():
    project_root = Path(__file__).resolve().parents[1]
    package = json.loads((project_root / "package.v3.json").read_text(encoding="utf-8"))

    icon = package["LunaTVSource"]["icon"]

    assert Path(icon).name == icon
    assert (project_root / "icons" / icon).is_file()
