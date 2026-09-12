from types import SimpleNamespace

import app.plugins.lunatvsource as plugin_module
import app.plugins.lunatvsource.classification as classification_module
from app.plugins.lunatvsource import LunaTVSource
from app.plugins.lunatvsource.cms import CmsEpisode, CmsResult


def test_classification_facts_are_stable_and_normalized():
    from app.plugins.lunatvsource.classification import (
        CMS_CLASS_NAMES_FIELD,
        CMS_SOURCE_KEY_FIELD,
        CMS_TYPE_NAME_FIELD,
        extract_classification_facts,
        normalize_cms_class_names,
    )

    assert normalize_cms_class_names("动作, 科幻/动作；悬疑") == (
        "动作",
        "科幻",
        "悬疑",
    )
    result = SimpleNamespace(
        source_key="source-a",
        cms_type_name="电视剧",
        cms_class_names=("动作", "科幻"),
    )
    assert extract_classification_facts(result) == {
        CMS_SOURCE_KEY_FIELD: "source-a",
        CMS_TYPE_NAME_FIELD: "电视剧",
        CMS_CLASS_NAMES_FIELD: ["动作", "科幻"],
    }


def test_media_source_declaration_is_exposed_when_host_protocol_is_available(
    monkeypatch,
):
    class Model:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    sdk = SimpleNamespace(
        ClassificationFieldDefinition=Model,
        MediaSourceInfo=Model,
    )
    monkeypatch.setattr(classification_module, "_classification_sdk", lambda: sdk)

    [declaration] = LunaTVSource().get_media_source()

    assert declaration.media_source == "lunatv"
    assert declaration.media_types == ["电影", "电视剧"]
    assert len(declaration.classification_fields) == 3


def test_native_resource_classification_reaches_download_task(monkeypatch, tmp_path):
    class TorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    result = CmsResult(
        source_key="demo",
        source_name="演示源",
        vod_id="series",
        title="分类示例剧",
        year="2026",
        media_type="tv",
        remark="",
        episodes=(
            CmsEpisode(1, 1, "第1集", "https://video.example/s01e01.m3u8"),
        ),
    )

    class Client:
        sources = ()

        @staticmethod
        def search(*_args, **_kwargs):
            return [result]

    media = SimpleNamespace(
        media_source="themoviedb",
        media_id="42",
        classification=SimpleNamespace(
            policy_revision="revision-7",
            effective=SimpleNamespace(
                category_id="tv/domestic",
                category_path=["电视剧", "国产剧"],
                rule_id="rule-1",
                source="policy",
            ),
        ),
    )
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "download_root": str(tmp_path)})
    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", TorrentInfo)
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_associate_tmdb", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(plugin, "_probe_resource_urls", lambda _urls: {})
    monkeypatch.setattr(plugin, "_start_queue", lambda: None)

    [torrent] = plugin.search_torrents(
        site={},
        keyword="分类示例剧",
        mtype="tv",
        media_source="themoviedb",
        media_id="42",
        media=media,
    )
    payload = plugin._decode_resource_token(torrent.enclosure)
    assert payload["classification"]["media_category"] == "电视剧/国产剧"
    assert payload["classification"]["classification_policy_revision"] == "revision-7"

    result_tuple = plugin.download(torrent.enclosure, tmp_path)
    assert result_tuple and result_tuple[1]
    [task] = plugin._queue.list_tasks()
    assert task["media_category_id"] == "tv/domestic"
    assert task["media_category"] == "电视剧/国产剧"
    assert task["classification_rule_id"] == "rule-1"
    assert task["classification_policy_revision"] == "revision-7"
    assert task["classification_source"] == "policy"
