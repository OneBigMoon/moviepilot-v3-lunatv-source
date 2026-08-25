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


def test_manifest_version_and_history_match_release_metadata():
    project_root = Path(__file__).resolve().parents[1]
    manifest = json.loads((project_root / "package.v3.json").read_text(encoding="utf-8"))["LunaTVSource"]
    package = json.loads(
        (project_root / "plugins.v3" / "lunatvsource" / "package.json").read_text(encoding="utf-8")
    )
    lockfile = json.loads(
        (project_root / "plugins.v3" / "lunatvsource" / "package-lock.json").read_text(encoding="utf-8")
    )

    expected_version = "0.4.40"
    assert manifest["version"] == expected_version
    assert LunaTVSource.plugin_version == expected_version
    assert package["version"] == expected_version
    assert lockfile["version"] == expected_version
    assert lockfile["packages"][""]["version"] == expected_version

    history = manifest["history"]
    assert next(iter(history)) == expected_version
    assert history["0.4.40"] == (
        "电视剧分集行完整聚合为季卡，补齐长季分页、稀疏详情和多组播放地址；"
        "电影/电视剧在每源限额前过滤；整季下载跳过坏地址并保留有效剧集；"
        "分辨率失败正确标记部分实测；队列保留全部非终态任务并修复删除持久化竞态。"
    )
    assert history["0.4.38"] == (
        "电视剧资源按季聚合，冲突同集自动选择最高画质；大季按首、中、末代表集实测并按该结果排序，"
        "未抽样集明确标记，资源搜索按媒体类型过滤，季订阅不再只刷新首集。"
    )
