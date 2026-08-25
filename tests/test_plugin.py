from lunatvsource_test import LunaTVSource
import lunatvsource_test as plugin_module
from lunatvsource_test.cms import (
    AppleCmsClient,
    CmsEpisode,
    CmsResult,
    CmsSource,
    _result_from_item,
)
from lunatvsource_test.downloader import DownloadTask
from lunatvsource_test.naming import media_path
from pathlib import Path
from collections.abc import Mapping
import hashlib
import pytest
import sys
import threading
from enum import Enum
from types import ModuleType, SimpleNamespace


def _field(value, key, default=None):
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def test_status_exposes_serial_queue_and_ai_fallback():
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "ai_enabled": False})
    status = plugin.api_status()["data"]
    assert status["enabled"] is True
    assert status["queue"]["pending"] == 0
    assert status["ai"]["enabled"] is True
    assert status["ai"]["available"] is False
    assert status["media_source"] == "lunatv"
    assert plugin.get_sidebar_nav() == []


def test_service_registers_subscription_refresh_and_serial_queue():
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "poll_minutes": 15, "queue_minutes": 2})
    services = plugin.get_service()
    assert {item["id"] for item in services} == {
        "LunaTVSource.Refresh",
        "LunaTVSource.DownloadQueue",
    }
    assert {item["func"] for item in services} == {
        plugin.refresh_subscriptions,
        plugin.run_queue,
    }


def test_refresh_subscriptions_does_not_use_legacy_operator_when_v3_operator_is_missing(monkeypatch):
    legacy_calls = []

    class LegacySubscribeOper:
        def list(self, state=None):
            legacy_calls.append(state)
            return []

    app_module = ModuleType("app")
    app_module.__path__ = []
    sdk_module = ModuleType("app.sdk")
    sdk_module.__path__ = []
    legacy_package = ModuleType("app.sdk._legacy")
    legacy_package.__path__ = []
    legacy_subscribe_module = ModuleType("app.sdk._legacy.subscribe")
    legacy_subscribe_module.SubscribeOper = LegacySubscribeOper
    app_module.sdk = sdk_module
    sdk_module._legacy = legacy_package
    legacy_package.subscribe = legacy_subscribe_module

    for module_name in (
        "app.db.oper.subscribe",
        "app.db.oper",
        "app.db",
    ):
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.setitem(sys.modules, "app", app_module)
    monkeypatch.setitem(sys.modules, "app.sdk", sdk_module)
    monkeypatch.setitem(sys.modules, "app.sdk._legacy", legacy_package)
    monkeypatch.setitem(sys.modules, "app.sdk._legacy.subscribe", legacy_subscribe_module)

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})

    assert plugin.refresh_subscriptions() == {
        "subscriptions": 0,
        "queued": 0,
        "reconciled": 0,
    }
    assert legacy_calls == []


def test_sources_fall_back_to_bundled_snapshot_when_remote_config_is_unreachable(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise OSError("network unreachable")

    monkeypatch.setattr(plugin_module, "load_sources_from_url", unavailable)
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})

    response = plugin.api_sources()

    assert response["success"] is True
    assert len(response["data"]) == 72
    assert {
        "url",
        "status",
        "status_label",
        "search_status",
        "search_label",
    }.issubset(response["data"][0])
    assert plugin._source_config_origin == "内置快照"
    assert plugin.api_status()["data"]["source_config"]["error"] == "network unreachable"


def test_sources_use_cached_snapshot_before_bundled_fallback(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise OSError("network unreachable")

    monkeypatch.setattr(plugin_module, "load_sources_from_url", unavailable)
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    plugin.save_data(
        plugin_module.SOURCE_CACHE_KEY,
        [{"key": "cached", "name": "缓存源", "api": "https://cached.example/vod"}],
    )

    response = plugin.api_sources()

    assert response["success"] is True
    assert response["data"] == [{
        "key": "cached",
        "name": "缓存源",
        "api": "https://cached.example/vod",
        "detail": "",
        "comment": "",
        "url": "https://cached.example/vod",
        "status": "ready",
        "status_label": "已加载",
        "search_status": "supported",
        "search_label": "支持",
    }]
    assert plugin._source_config_origin == "本地缓存"


def test_manual_download_rejects_non_http_url():
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "download_root": "/tmp/lunatv-test"})
    result = plugin.api_download({"url": "file:///tmp/movie.m3u8"})
    assert result["success"] is False
    assert "http/https" in result["message"]


def test_manual_download_wakes_queue_once_only_for_new_task(monkeypatch, tmp_path: Path):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "download_root": str(tmp_path)})
    wakeups = []
    monkeypatch.setattr(plugin, "_start_queue", lambda: wakeups.append(True))
    payload = {
        "url": "https://example.test/manual.m3u8",
        "title": "手动下载",
        "year": "2026",
        "media_type": "movie",
    }

    assert plugin.api_download(payload)["success"] is True
    assert plugin.api_download(payload)["success"] is False
    assert wakeups == [True]


def test_api_search_expands_episode_rows_for_downloadable_results(monkeypatch):
    calls = []

    class Client:
        def search(self, query, **kwargs):
            calls.append((query, kwargs))
            return []

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin, "_client", lambda: Client())

    response = plugin.api_search({"query": "示例剧"})

    assert response == {"success": True, "data": []}
    assert calls and calls[0][1]["expand_tv_episode_rows"] is True


def test_api_download_enqueues_lunatv_season_token_once(monkeypatch, tmp_path: Path):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "download_root": str(tmp_path)})
    wakeups = []
    monkeypatch.setattr(plugin, "_start_queue", lambda: wakeups.append(True))
    monkeypatch.setattr(
        plugin,
        "_client",
        lambda: (_ for _ in ()).throw(AssertionError("API download must not search CMS")),
    )
    token = plugin._resource_token(
        {
            "url": "https://example.test/s01e01.m3u8",
            "title": "小猪佩奇",
            "year": "2004",
            "media_type": "tv",
            "season": 1,
            "episode": 1,
            "media_id": "demo:42",
            "source_key": "demo",
            "episodes": [
                {
                    "url": "https://example.test/s01e01.m3u8",
                    "season": 1,
                    "episode": 1,
                },
                {
                    "url": "https://example.test/s01e02.m3u8",
                    "season": 1,
                    "episode": 2,
                },
            ],
        }
    )

    first = plugin.api_download({"content": token})
    duplicate = plugin.api_download({"enclosure": token})

    assert first["success"] is True
    assert first["data"]["task_id"]
    assert "已排队 2 集" in first["message"]
    assert duplicate["success"] is False
    assert duplicate["data"]["task_id"] is None
    assert [(task["season"], task["episode"], task["url"])
            for task in sorted(plugin._queue.list_tasks(), key=lambda item: item["episode"])] == [
        (1, 1, "https://example.test/s01e01.m3u8"),
        (1, 2, "https://example.test/s01e02.m3u8"),
    ]
    assert wakeups == [True]


def test_api_download_encodes_top_level_episodes_for_native_season_download(
    monkeypatch, tmp_path: Path
):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "download_root": str(tmp_path)})
    wakeups = []
    monkeypatch.setattr(plugin, "_start_queue", lambda: wakeups.append(True))

    response = plugin.api_download(
        {
            "title": "示例剧",
            "year": "2026",
            "media_type": "tv",
            "media_id": "demo:7",
            "source_key": "demo",
            "root": str(tmp_path),
            "episodes": [
                {
                    "url": "https://example.test/s02e01.m3u8",
                    "season": 2,
                    "episode": 1,
                },
                {
                    "url": "https://example.test/s02e02.m3u8",
                    "season": 2,
                    "episode": 2,
                },
            ],
        }
    )

    assert response["success"] is True
    assert response["data"]["task_id"]
    assert [(task["season"], task["episode"])
            for task in sorted(plugin._queue.list_tasks(), key=lambda item: item["episode"])] == [
        (2, 1),
        (2, 2),
    ]
    assert wakeups == [True]


def test_api_download_rejects_non_lunatv_resource_token(monkeypatch, tmp_path: Path):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "download_root": str(tmp_path)})
    monkeypatch.setattr(
        plugin,
        "_client",
        lambda: (_ for _ in ()).throw(AssertionError("API download must not search CMS")),
    )

    response = plugin.api_download(
        {"content": "magnet:?xt=urn:btih:not-a-lunatv-resource", "root": str(tmp_path)}
    )

    assert response["success"] is False
    assert "LunaTV 资源令牌" in response["message"]
    assert response["data"] == {"task_id": None}
    assert plugin._queue.list_tasks() == []


def test_api_download_keeps_single_url_path_when_episodes_are_empty(
    monkeypatch, tmp_path: Path
):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "download_root": str(tmp_path)})
    wakeups = []
    monkeypatch.setattr(plugin, "_start_queue", lambda: wakeups.append(True))

    def native_download_must_not_run(*_args, **_kwargs):
        raise AssertionError("empty episodes must keep the direct URL path")

    monkeypatch.setattr(plugin, "download", native_download_must_not_run)

    response = plugin.api_download(
        {
            "url": "https://example.test/movie.m3u8",
            "title": "示例电影",
            "media_type": "movie",
            "episodes": [],
        }
    )

    assert response["success"] is True
    assert response["data"]["task_id"]
    assert [task["url"] for task in plugin._queue.list_tasks()] == [
        "https://example.test/movie.m3u8"
    ]
    assert wakeups == [True]


def test_api_download_empty_episode_token_falls_back_to_single_resource(
    monkeypatch, tmp_path: Path
):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "download_root": str(tmp_path)})
    wakeups = []
    monkeypatch.setattr(plugin, "_start_queue", lambda: wakeups.append(True))
    token = plugin._resource_token(
        {
            "url": "https://example.test/movie.m3u8",
            "title": "示例电影",
            "media_type": "movie",
            "episodes": [],
        }
    )

    native_download = plugin.download

    def download_single_resource(content, *args, **kwargs):
        assert "episodes" not in plugin._decode_resource_token(content)
        return native_download(content, *args, **kwargs)

    monkeypatch.setattr(plugin, "download", download_single_resource)

    response = plugin.api_download({"content": token})

    assert response["success"] is True
    assert response["data"]["task_id"]
    assert [task["url"] for task in plugin._queue.list_tasks()] == [
        "https://example.test/movie.m3u8"
    ]
    assert wakeups == [True]


@pytest.mark.parametrize("as_token", [False, True], ids=["top-level", "token"])
def test_api_download_season_skips_invalid_entries_before_later_valid_entry(
    monkeypatch, tmp_path: Path, as_token: bool
):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "download_root": str(tmp_path)})
    wakeups = []
    monkeypatch.setattr(plugin, "_start_queue", lambda: wakeups.append(True))
    resource = {
        "url": "file:///tmp/not-a-stream.m3u8",
        "title": "示例剧",
        "media_type": "tv",
        "episodes": [
            None,
            {"url": "file:///tmp/not-an-http-stream.m3u8", "season": 1, "episode": 1},
            {"url": "https://example.test/s01e02.m3u8", "season": 1, "episode": 2},
        ],
    }

    response = plugin.api_download(
        {"content": plugin._resource_token(resource)} if as_token else resource
    )

    assert response["success"] is True
    assert "2 集参数无效" in response["message"]
    assert [task["url"] for task in plugin._queue.list_tasks()] == [
        "https://example.test/s01e02.m3u8"
    ]
    assert wakeups == [True]


@pytest.mark.parametrize("as_token", [False, True], ids=["top-level", "token"])
def test_api_download_rejects_nonempty_all_invalid_episode_list(
    monkeypatch, tmp_path: Path, as_token: bool
):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "download_root": str(tmp_path)})
    wakeups = []
    monkeypatch.setattr(plugin, "_start_queue", lambda: wakeups.append(True))
    resource = {
        "url": "https://example.test/must-not-fall-back.m3u8",
        "title": "示例剧",
        "media_type": "tv",
        "episodes": [None, {"url": ""}],
    }

    response = plugin.api_download(
        {"content": plugin._resource_token(resource)} if as_token else resource
    )

    assert response["success"] is False
    assert response["data"] == {"task_id": None}
    assert plugin._queue.list_tasks() == []
    assert wakeups == []


def test_api_download_token_requires_valid_effective_root(monkeypatch):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin, "_effective_root", lambda **_kwargs: "")
    token = plugin._resource_token(
        {
            "url": "https://example.test/movie.m3u8",
            "title": "示例电影",
            "media_type": "movie",
        }
    )

    response = plugin.api_download({"content": token})

    assert response == {
        "success": False,
        "message": "未找到下载目录，请先配置插件目录或 MoviePilot 目录设置",
        "data": {"task_id": None},
    }


def test_directory_settings_are_used_when_plugin_root_is_empty(monkeypatch):
    class Directory:
        storage = "local"
        download_path = "/media/courses"
        library_path = "/media/library/courses"
        media_type = "电视剧"
        priority = 1
        name = "课程目录"

    class DirectoryHelper:
        def get_download_dirs(self):
            return [Directory()]

    monkeypatch.setattr(plugin_module, "_HostDirectoryHelper", DirectoryHelper)
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "use_moviepilot_dirs": False})
    assert plugin._effective_root(media_type="tv") == "/media/courses"
    assert plugin.api_status()["data"]["directories"]["source"] == "MoviePilot 目录设置"


def test_tmdb_association_can_map_flat_seasons(monkeypatch):
    class Source:
        TMDB = "themoviedb"

    class Meta:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Media:
        media_id = "123"
        tmdb_id = 123
        media_source = "themoviedb"
        title = "示例剧"
        year = "2024"
        seasons = {1: [1, 2], 2: [1]}

    class MediaChain:
        def recognize_media(self, **kwargs):
            return Media()

        def search_medias(self, **kwargs):
            return [Media()]

    monkeypatch.setattr(plugin_module, "_HostMediaSource", Source)
    monkeypatch.setattr(plugin_module, "_HostMetaInfo", Meta)
    monkeypatch.setattr(plugin_module, "_HostMediaChain", MediaChain)
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "tmdb_association": False})
    result = _result_from_item(
        CmsSource("demo", "演示", "https://cms.example/vod"),
        {
            "vod_id": "1",
            "vod_name": "示例剧 1-2季",
            "type_name": "电视剧",
            "vod_play_from": "在线播放",
            "vod_play_url": "01$https://example.test/01.m3u8#02$https://example.test/02.m3u8#03$https://example.test/03.m3u8",
        },
    )
    prepared, association = plugin._prepare_result(result)
    assert association["status"] == "matched"
    assert association["candidates"][0]["media_id"] == "123"
    assert [(episode.season, episode.episode) for episode in prepared.episodes] == [(1, 1), (1, 2), (2, 1)]


def test_tmdb_candidate_search_returns_compact_choices(monkeypatch):
    class Source:
        TMDB = "themoviedb"

    class Meta:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Media:
        media_id = "456"
        tmdb_id = 456
        media_source = "themoviedb"
        title = "候选作品"
        year = "2023"
        type = "电影"
        season = None
        seasons = {}

    class MediaChain:
        def search_medias(self, **kwargs):
            return [Media(), Media()]

    monkeypatch.setattr(plugin_module, "_HostMediaSource", Source)
    monkeypatch.setattr(plugin_module, "_HostMetaInfo", Meta)
    monkeypatch.setattr(plugin_module, "_HostMediaChain", MediaChain)
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "tmdb_association": True})
    response = plugin.api_tmdb_search({"title": "候选作品", "media_type": "movie"})
    assert response["success"] is True
    assert response["data"] == [{
        "media_source": "themoviedb",
        "media_id": "456",
        "tmdb_id": 456,
        "title": "候选作品",
        "year": "2023",
        "type": "电影",
        "season": None,
        "season_counts": {},
    }]


def test_resource_tmdb_association_skips_candidate_lookup_and_reuses_cache(monkeypatch):
    class Source:
        TMDB = "themoviedb"

    class Meta:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Media:
        media_id = "456"
        tmdb_id = 456
        media_source = "themoviedb"
        title = "示例电影"
        year = "2024"
        seasons = {}

    recognize_calls = []
    candidate_calls = []

    class MediaChain:
        def recognize_media(self, **kwargs):
            recognize_calls.append(kwargs)
            return Media()

        def search_medias(self, **kwargs):
            candidate_calls.append(kwargs)
            return [Media()]

    monkeypatch.setattr(plugin_module, "_HostMediaSource", Source)
    monkeypatch.setattr(plugin_module, "_HostMetaInfo", Meta)
    monkeypatch.setattr(plugin_module, "_HostMediaChain", MediaChain)
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    result = CmsResult(
        source_key="demo",
        source_name="演示",
        vod_id="42",
        title="示例电影",
        year="2024",
        media_type="movie",
        remark="",
    )

    first = plugin._associate_tmdb(result, include_candidates=False)
    second = plugin._associate_tmdb(result, include_candidates=False)

    assert first["media_id"] == "456"
    assert second["media_id"] == "456"
    assert len(recognize_calls) == 1
    assert candidate_calls == []


def test_host_meta_info_uses_v3_function_signature(monkeypatch):
    calls = []

    def meta_info(*, title):
        calls.append(title)
        return type("Meta", (), {"type": "电影"})()

    monkeypatch.setattr(plugin_module, "_HostMetaInfo", meta_info)
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    meta = plugin._host_meta_info("示例作品", "2024")
    assert calls == ["示例作品 (2024)"]
    assert meta.type == "电影"


def test_discover_accepts_native_keyword_and_stops_after_first_source(monkeypatch):
    calls = []

    class Client:
        def search(self, query, **kwargs):
            calls.append((query, kwargs))
            return []

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    response = plugin.api_discover(keyword="示例电影")
    assert response == {"success": True, "data": []}
    assert calls == [
        (
            "示例电影",
            {"limit": 30, "stop_after_first_source": True, "enrich": False},
        )
    ]


def test_global_media_search_returns_lunatv_cards_without_explore_tab(monkeypatch):
    class Client:
        def search(self, query, **kwargs):
            assert query == "示例电影"
            assert kwargs == {"limit": 8, "stop_after_first_source": True, "enrich": False}
            return [_result_from_item(
                CmsSource("demo", "演示源", "https://cms.example/vod"),
                {"vod_id": "42", "vod_name": "示例电影", "type_name": "电影"},
            )]

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_prepare_result", lambda result: (result, {}))
    monkeypatch.setattr(plugin, "_media_info", lambda result, association: result)
    meta = type("Meta", (), {"name": "示例电影", "year": "", "type": "电影"})()
    results = plugin.search_medias(meta=meta)
    assert len(results) == 1
    assert results[0].title == "示例电影"
    assert plugin.get_media_source() == []


def test_global_media_search_respects_explicit_other_source():
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    meta = type("Meta", (), {"name": "示例电影"})()
    assert plugin.search_medias(meta=meta, media_source=("themoviedb",)) == []


def test_native_resource_search_returns_marked_download_items(monkeypatch):
    class TorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        def to_dict(self):
            return dict(self.__dict__)

    class Client:
        def search(self, query, **kwargs):
            return [_result_from_item(
                CmsSource("demo", "演示源", "https://cms.example/vod", "https://cms.example"),
                {
                    "vod_id": "42",
                    "vod_name": "示例剧",
                    "vod_year": "2024",
                    "type_name": "电视剧",
                    "vod_play_from": "在线播放",
                    "vod_play_url": "01$https://example.test/01.m3u8",
                },
            )]

    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", TorrentInfo)
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    association_calls = []

    def associate(context, include_candidates=True):
        association_calls.append((context, include_candidates))
        return {"media_source": "themoviedb", "media_id": "123"}

    monkeypatch.setattr(plugin, "_associate_tmdb", associate)
    items = plugin.search_torrents(site={"id": 1}, keyword="示例剧", page=0)
    assert len(items) == 1
    assert items[0].site_name == "演示源"
    assert items[0].to_dict()["site_name"] == "演示源"
    assert items[0].media_source == "themoviedb"
    assert items[0].media_id == "123"
    assert items[0].title.endswith("第1季 · 未知")
    assert "集" not in items[0].title
    assert items[0].description == "LunaTV · 第1季 · 未知 · m3u8 · 共1集 · 已测0/1集"
    assert "未知" in items[0].labels
    payload = plugin._decode_resource_token(items[0].enclosure)
    assert payload["url"].endswith("01.m3u8")
    assert len(payload["episodes"]) == 1
    assert payload["episodes"][0]["episode"] == 1
    assert payload["source_key"] == "demo"
    assert payload["source_name"] == "演示源"
    assert payload["host_media_source"] == "themoviedb"
    assert payload["host_media_id"] == "123"
    assert [(item.title, item.year, item.media_type, include_candidates)
            for item, include_candidates in association_calls] == [
        ("示例剧", "2024", "tv", False),
    ]


def test_resource_torrents_forwards_lunatv_progress_callback(monkeypatch):
    class TorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    calls = []

    class Client:
        def search(self, query, **kwargs):
            calls.append((query, kwargs))
            callback = kwargs["progress_callback"]
            callback(finished=1, total=2, text="CMS 1/2")
            callback(finished=2, total=2, text="CMS 2/2")
            return []

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    progress = []

    def on_progress(**event):
        progress.append(event)
        if event["finished"] == 1:
            raise RuntimeError("broken host callback")

    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", TorrentInfo)
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_associate_tmdb", lambda *_args, **_kwargs: {})

    assert plugin._resource_torrents("progress demo", progress_callback=on_progress) == []
    assert calls[0][0] == "progress demo"
    assert set(calls[0][1]) == {
        "limit",
        "source_limit",
        "stop_after_first_source",
        "require_playable",
        "expand_tv_episode_rows",
        "max_workers",
        "progress_callback",
    }
    assert [(event["finished"], event["total"], event["text"]) for event in progress] == [
        (1, 2, "LunaTV 正在搜索源 1/2"),
        (2, 2, "LunaTV 正在搜索源 2/2"),
        (2, 2, "LunaTV 正在汇总资源并检测清晰度"),
        (2, 2, "LunaTV 正在按清晰度排序"),
    ]


def test_search_torrent_entrypoints_forward_progress_callback(monkeypatch):
    import asyncio

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    received = []
    callback = lambda **_event: None

    def resource_torrents(keyword, mtype=None, progress_callback=None):
        received.append((keyword, mtype, progress_callback))
        return ["luna"]

    monkeypatch.setattr(plugin, "_resource_torrents", resource_torrents)

    assert plugin.search_torrents(
        site={},
        keyword="sync demo",
        mtype="tv",
        progress_callback=callback,
    ) == ["luna"]
    assert asyncio.run(
        plugin.async_search_torrents(
            site={},
            keyword="async demo",
            mtype="movie",
            progress_callback=callback,
        )
    ) == ["luna"]
    assert received == [
        ("sync demo", "tv", callback),
        ("async demo", "movie", callback),
    ]


def test_resource_torrents_groups_by_source_and_season(monkeypatch):
    calls = []
    ai_calls = []
    association_calls = []

    class TorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        def to_dict(self):
            return dict(self.__dict__)

    class Client:
        def search(self, query, **kwargs):
            calls.append(kwargs)
            return [
                _result_from_item(
                    CmsSource("first", "源A", "https://cms.example/vod", "https://cms.example"),
                    {
                        "vod_id": "1",
                        "vod_name": "示例剧",
                        "type_name": "电视剧",
                        "vod_play_from": "在线播放",
                        "vod_play_url": "01$https://example.test/01.m3u8",
                    },
                ),
                _result_from_item(
                    CmsSource("second", "源B", "https://cms2.example/vod", "https://cms2.example"),
                    {
                        "vod_id": "2",
                        "vod_name": "示例剧",
                        "type_name": "电视剧",
                        "vod_play_from": "在线播放",
                        "vod_play_url": "01$https://example.test/01.m3u8",
                    },
                ),
                _result_from_item(
                    CmsSource("first", "源A", "https://cms.example/vod", "https://cms.example"),
                    {
                        "vod_id": "3",
                        "vod_name": "示例剧",
                        "type_name": "电视剧",
                        "vod_play_from": "在线播放",
                        "vod_play_url": "02$https://example.test/02.m3u8",
                    },
                ),
            ]

    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", TorrentInfo)
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin, "_client", lambda: Client())

    class Ai:
        def normalize(self, title, year="", media_type=""):
            ai_calls.append((title, year, media_type))
            return "标准示例剧", "ai"

    def associate(context, include_candidates=True):
        association_calls.append((context, include_candidates))
        return {
            "status": "matched",
            "media_source": "themoviedb",
            "media_id": "123",
            "season_counts": {1: 2},
        }

    plugin._ai = Ai()
    monkeypatch.setattr(plugin, "_associate_tmdb", associate)
    items = plugin.search_torrents(site={"id": 1}, keyword="示例剧", page=0, mtype="tv")
    assert len(items) == 2
    payloads = [plugin._decode_resource_token(item.enclosure) for item in items]
    assert [item.site_name for item in items] == ["源A", "源B"]
    assert [len(payload["episodes"]) for payload in payloads] == [2, 1]
    assert [
        [episode["url"] for episode in payload["episodes"]]
        for payload in payloads
    ] == [["https://example.test/01.m3u8", "https://example.test/02.m3u8"],
          ["https://example.test/01.m3u8"]]
    assert all("· 第1季 · " in item.title and "集" not in item.title for item in items)
    assert calls == [{
        "limit": 50,
        "source_limit": 3,
        "stop_after_first_source": False,
        "require_playable": True,
        "expand_tv_episode_rows": True,
        "max_workers": 8,
        "media_type_filter": "tv",
    }]
    assert ai_calls == [("示例剧", "", "")]
    assert [(item.title, item.year, item.media_type, include_candidates)
            for item, include_candidates in association_calls] == [
        ("标准示例剧", "", "tv", False),
    ]
    assert {(payload["host_media_source"], payload["host_media_id"])
            for payload in payloads} == {("themoviedb", "123")}


def test_resource_torrents_collapses_episode_named_cms_rows_into_one_season(monkeypatch):
    class TorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    source = CmsSource("peppa", "极速资源", "https://cms.example/vod", "https://cms.example")
    rows = [
        _result_from_item(source, {
            "vod_id": "1",
            "vod_name": "小猪佩奇 第一季 第1集",
            "type_name": "欧美动漫",
            "vod_play_url": "第1集$https://example.test/peppa-01.m3u8",
        }),
        _result_from_item(source, {
            "vod_id": "2",
            "vod_name": "小猪佩奇 第一季 第2集",
            "type_name": "欧美动漫",
            "vod_play_url": "第2集$https://example.test/peppa-02.m3u8",
        }),
    ]

    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", TorrentInfo)
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin, "_client", lambda: type("Client", (), {
        "search": lambda self, *_args, **_kwargs: rows,
    })())
    monkeypatch.setattr(plugin, "_associate_tmdb", lambda *_args, **_kwargs: {})

    items = plugin.search_torrents(site={"id": 1}, keyword="小猪佩奇", page=0, mtype="tv")
    assert len(items) == 1
    assert items[0].title == "小猪佩奇 · 第1季 · 未知"
    payload = plugin._decode_resource_token(items[0].enclosure)
    assert [episode["episode"] for episode in payload["episodes"]] == [1, 2]


def test_resource_torrents_label_and_prefer_verified_resolution(monkeypatch):
    class TorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    low = _result_from_item(
        CmsSource("low", "标清源", "https://low.example/vod"),
        {
            "vod_id": "1",
            "vod_name": "示例电影",
            "type_name": "电影",
            "vod_play_url": "正片$https://video.example/480.m3u8",
        },
    )
    high = _result_from_item(
        CmsSource("high", "高清源", "https://high.example/vod"),
        {
            "vod_id": "2",
            "vod_name": "示例电影",
            "type_name": "电影",
            "vod_play_url": "正片$https://video.example/1080.m3u8",
        },
    )

    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", TorrentInfo)
    monkeypatch.setattr(
        plugin_module,
        "probe_stream_height",
        lambda url, **_kwargs: 1080 if "1080" in url else 480,
    )
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin, "_client", lambda: type("Client", (), {
        "search": lambda self, *_args, **_kwargs: [low, high],
    })())
    monkeypatch.setattr(plugin, "_associate_tmdb", lambda *_args, **_kwargs: {})

    items = plugin.search_torrents(site={"id": 1}, keyword="示例电影", page=0, mtype="movie")

    assert [item.site_name for item in items] == ["高清源", "标清源"]
    assert [item.pri_order for item in items] == [108, 48]
    assert items[0].title.endswith("· 1080P")
    assert items[0].description == "LunaTV · 1080P · m3u8"
    assert "1080P" in items[0].labels
    assert plugin._decode_resource_token(items[0].enclosure)["resolution"] == "1080P"


def test_global_media_search_collapses_episode_rows_into_season_cards(monkeypatch):
    source = CmsSource("demo", "演示源", "https://cms.example/vod")
    rows = [
        _result_from_item(
            source,
            {
                "vod_id": "s1e1",
                "vod_name": "小猪佩奇 第一季 第1集",
                "vod_year": "2004",
                "type_name": "电视剧",
                "vod_play_url": "第1集$https://video.example/s1e1.m3u8",
            },
        ),
        _result_from_item(
            source,
            {
                "vod_id": "s1e2",
                "vod_name": "小猪佩奇 第一季 第2集",
                "vod_year": "2004",
                "type_name": "电视剧",
                "vod_play_url": "第2集$https://video.example/s1e2.m3u8",
            },
        ),
        _result_from_item(
            source,
            {
                "vod_id": "s2e1",
                "vod_name": "小猪佩奇 第二季 第1集",
                "vod_year": "2004",
                "type_name": "电视剧",
                "vod_play_url": "第1集$https://video.example/s2e1.m3u8",
            },
        ),
    ]

    class Client:
        def search(self, *_args, **_kwargs):
            return rows

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_prepare_result", lambda result: (result, {}))

    meta = type("Meta", (), {"name": "小猪佩奇", "year": "", "type": "电视剧"})()
    cards = plugin.search_medias(meta=meta)
    discovered = plugin.api_discover(keyword="小猪佩奇")["data"]

    for projected in (cards, discovered):
        assert len(projected) == 2
        assert [_field(item, "title") for item in projected] == ["小猪佩奇", "小猪佩奇"]
        assert [_field(item, "seasons") for item in projected] == [{1: []}, {2: []}]
        assert all(_field(item, "episodes", []) == [] for item in projected)


def test_global_media_search_keeps_each_ambiguous_range_season(monkeypatch):
    source = CmsSource("demo", "演示源", "https://cms.example/vod")
    flat = _result_from_item(
        source,
        {
            "vod_id": "bundle",
            "vod_name": "示例剧 1-3季",
            "vod_year": "2024",
            "type_name": "电视剧",
            "vod_play_url": (
                "01$https://video.example/01.m3u8#"
                "02$https://video.example/02.m3u8#"
                "03$https://video.example/03.m3u8"
            ),
        },
    )
    assert flat.season_ambiguous is True

    class Client:
        def search(self, *_args, **_kwargs):
            return [flat]

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_prepare_result", lambda result: (result, {}))

    meta = type("Meta", (), {"name": "示例剧", "year": "", "type": "电视剧"})()
    cards = plugin.search_medias(meta=meta)

    assert [_field(item, "seasons") for item in cards] == [{1: []}, {2: []}, {3: []}]
    assert all(_field(item, "episodes", []) == [] for item in cards)
    assert all(_field(item, "season_ambiguous", True) is True for item in cards)


def test_season_media_cards_are_not_order_dependent_when_precise_row_exists():
    ambiguous = CmsResult(
        source_key="demo",
        source_name="演示源",
        vod_id="bundle",
        title="示例剧",
        year="2024",
        media_type="tv",
        remark="",
        episodes=(),
        season_range=(1, 1),
        season_ambiguous=True,
    )
    precise = CmsResult(
        source_key="demo",
        source_name="演示源",
        vod_id="episode-1",
        title="示例剧",
        year="2024",
        media_type="tv",
        remark="",
        episodes=(
            CmsEpisode(1, 1, "第1集", "https://video.example/s01e01.m3u8"),
        ),
        season_range=(0, 0),
        season_ambiguous=False,
    )

    for rows in ([ambiguous, precise], [precise, ambiguous]):
        cards = LunaTVSource._season_media_cards(rows)

        assert len(cards) == 1
        assert cards[0].season_ambiguous is False
        assert [(item.season, item.episode) for item in cards[0].episodes] == [(1, 1)]


def test_quality_cache_prunes_expired_entries_and_enforces_capacity(monkeypatch):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin_module.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(plugin_module, "probe_stream_height", lambda *_args, **_kwargs: 1080)
    plugin._quality_cache = {
        "expired": (0.0, 1080),
        **{
            f"https://video.example/{index}.m3u8": (999.0 - index / 10000, 1080)
            for index in range(plugin_module._QUALITY_CACHE_MAX_ENTRIES + 20)
        },
    }

    assert plugin._probe_quality("https://video.example/new.m3u8") == 1080
    assert "expired" not in plugin._quality_cache
    assert len(plugin._quality_cache) <= plugin_module._QUALITY_CACHE_MAX_ENTRIES


def test_quality_probe_passes_explicit_private_network_allowlist(monkeypatch):
    captured = {}

    def probe(*_args, **kwargs):
        captured.update(kwargs)
        return 1080

    plugin = LunaTVSource()
    plugin.init_plugin(
        {
            "enabled": True,
            "probe_allowed_private_ranges": "10.0.0.0/8, 192.168.0.0/16",
        }
    )
    monkeypatch.setattr(plugin_module, "probe_stream_height", probe)

    assert plugin._probe_quality("http://10.0.0.8/video.m3u8") == 1080
    assert captured["allowed_private_ranges"] == (
        "10.0.0.0/8",
        "192.168.0.0/16",
    )


def test_resource_search_cache_prunes_expired_entries_and_enforces_capacity():
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    plugin._resource_search_cache = {
        "expired": (0.0, []),
        **{
            f"fresh-{index}": (999.0 - index / 10000, [])
            for index in range(plugin_module._RESOURCE_SEARCH_CACHE_MAX_ENTRIES + 20)
        },
    }

    plugin._prune_resource_search_cache(1000.0)

    assert "expired" not in plugin._resource_search_cache
    assert (
        len(plugin._resource_search_cache)
        <= plugin_module._RESOURCE_SEARCH_CACHE_MAX_ENTRIES
    )


def test_tmdb_cache_enforces_capacity_and_keeps_latest_entry():
    plugin = LunaTVSource()
    plugin._tmdb_cache = {
        f"old-{index}": {"status": "matched", "media_id": str(index)}
        for index in range(plugin_module._TMDB_CACHE_MAX_ENTRIES + 20)
    }

    plugin._store_tmdb_cache_entry(
        "latest",
        {"status": "matched", "media_id": "latest"},
    )

    assert len(plugin._tmdb_cache) == plugin_module._TMDB_CACHE_MAX_ENTRIES
    assert "old-0" not in plugin._tmdb_cache
    assert plugin._tmdb_cache["latest"]["media_id"] == "latest"


def test_tmdb_cache_persists_newer_same_key_snapshot_after_race(monkeypatch):
    """The snapshot that is written last must not be older than the cache."""

    plugin = LunaTVSource()
    plugin._tmdb_cache = {}
    first_lock_exit = threading.Event()
    new_snapshot_saved = threading.Event()
    writes = {}
    thread_errors = []

    class FirstExitWaitsForNewSnapshot:
        """Let the old implementation expose its post-lock save window.

        The first cache mutation releases this lock before it may continue to
        ``save_data``.  A second mutation then persists a newer snapshot.  If
        saving occurs outside the cache lock, the first mutation overwrites it
        afterwards; with the save inside the lock, the newer snapshot is last.
        """

        def __init__(self):
            self._lock = threading.RLock()
            self._exit_lock = threading.Lock()
            self._exit_count = 0

        def __enter__(self):
            self._lock.acquire()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            with self._exit_lock:
                first_exit = self._exit_count == 0
                self._exit_count += 1
            self._lock.release()
            if first_exit:
                first_lock_exit.set()
                if not new_snapshot_saved.wait(timeout=2):
                    raise AssertionError("newer TMDB cache snapshot was not saved")
            return False

    def save_data(key, snapshot):
        marker = snapshot["same-key"]["marker"]
        writes[key] = dict(snapshot)
        if marker == "new":
            new_snapshot_saved.set()

    def store(marker):
        try:
            plugin._store_tmdb_cache_entry(
                "same-key", {"status": "matched", "marker": marker}
            )
        except BaseException as error:
            thread_errors.append(error)

    monkeypatch.setattr(plugin, "_tmdb_cache_lock", FirstExitWaitsForNewSnapshot())
    monkeypatch.setattr(plugin, "save_data", save_data)

    old_thread = threading.Thread(target=store, args=("old",), daemon=True)
    old_thread.start()
    assert first_lock_exit.wait(timeout=2)

    new_thread = threading.Thread(target=store, args=("new",), daemon=True)
    new_thread.start()
    assert new_snapshot_saved.wait(timeout=2)
    old_thread.join(timeout=2)
    new_thread.join(timeout=2)

    assert not old_thread.is_alive()
    assert not new_thread.is_alive()
    assert thread_errors == []
    assert writes["tmdb_match_cache_v1"]["same-key"]["marker"] == "new"


def test_tmdb_cache_lock_recovers_after_save_data_error(monkeypatch):
    plugin = LunaTVSource()
    plugin._tmdb_cache = {}
    calls = 0
    writes = {}

    def save_data(key, snapshot):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("persistent store unavailable")
        writes[key] = dict(snapshot)

    monkeypatch.setattr(plugin, "save_data", save_data)

    with pytest.raises(RuntimeError, match="persistent store unavailable"):
        plugin._store_tmdb_cache_entry(
            "same-key", {"status": "matched", "marker": "failed"}
        )

    completed = threading.Event()
    thread_errors = []

    def store_after_failure():
        try:
            plugin._store_tmdb_cache_entry(
                "same-key", {"status": "matched", "marker": "recovered"}
            )
        except BaseException as error:
            thread_errors.append(error)
        finally:
            completed.set()

    recovery_thread = threading.Thread(target=store_after_failure, daemon=True)
    recovery_thread.start()
    assert completed.wait(timeout=2)
    recovery_thread.join(timeout=2)

    assert not recovery_thread.is_alive()
    assert thread_errors == []
    assert writes["tmdb_match_cache_v1"]["same-key"]["marker"] == "recovered"


def test_media_info_keeps_precise_episode_details_outside_search_projection(monkeypatch):
    monkeypatch.setattr(plugin_module, "_schemas", None)
    result = CmsResult(
        source_key="demo",
        source_name="演示源",
        vod_id="42",
        title="示例剧",
        year="2024",
        media_type="tv",
        remark="",
        episodes=(
            CmsEpisode(1, 1, "第1集", "https://video.example/s1e1.m3u8"),
            CmsEpisode(2, 3, "第3集", "https://video.example/s2e3.m3u8"),
        ),
    )

    projected = LunaTVSource()._media_info(result)

    assert [(item["season"], item["episode"]) for item in projected["episodes"]] == [
        (1, 1),
        (2, 3),
    ]


def test_resource_torrents_skip_unknown_tv_season_instead_of_episode_fallback(monkeypatch):
    class TorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    unknown = CmsResult(
        source_key="demo",
        source_name="演示源",
        vod_id="unknown",
        title="未知季剧集",
        year="2024",
        media_type="tv",
        remark="",
        episodes=(
            CmsEpisode(
                season=1,
                episode=1,
                label="第1集",
                url="https://video.example/unknown-s01e01.m3u8",
                season_known=False,
            ),
        ),
        season_range=(0, 0),
    )

    class Client:
        def search(self, *_args, **_kwargs):
            return [unknown]

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", TorrentInfo)
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_associate_tmdb", lambda *_args, **_kwargs: {})

    assert plugin._resource_torrents("未知季剧集") == []


def test_resource_torrents_enables_episode_row_expansion(monkeypatch):
    calls = []

    class TorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Client:
        def search(self, *_args, **kwargs):
            calls.append(kwargs)
            return []

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", TorrentInfo)
    monkeypatch.setattr(plugin, "_client", lambda: Client())

    assert plugin._resource_torrents("长剧") == []
    assert calls and calls[0]["expand_tv_episode_rows"] is True


def test_resource_torrents_expands_limit_for_each_configured_source(monkeypatch):
    calls = []

    class TorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Client:
        sources = [object() for _ in range(65)]

        def search(self, *_args, **kwargs):
            calls.append(kwargs)
            return []

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", TorrentInfo)
    monkeypatch.setattr(plugin, "_client", lambda: Client())

    assert plugin._resource_torrents("长剧") == []
    assert calls and calls[0]["source_limit"] == 3
    assert calls[0]["limit"] == 65 * 3


def test_resource_torrents_keep_movie_single_and_season_free(monkeypatch):
    class TorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    movie = _result_from_item(
        CmsSource("demo", "演示源", "https://cms.example/vod"),
        {
            "vod_id": "movie",
            "vod_name": "示例电影",
            "vod_year": "2024",
            "type_name": "电影",
            "vod_play_url": "正片$https://video.example/movie.m3u8",
        },
    )

    class Client:
        def search(self, *_args, **_kwargs):
            return [movie]

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", TorrentInfo)
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_associate_tmdb", lambda *_args, **_kwargs: {})

    items = plugin._resource_torrents("示例电影")

    assert len(items) == 1
    assert items[0].category == "电影"
    assert "季" not in items[0].title
    assert all("季" not in label for label in items[0].labels)
    assert plugin._decode_resource_token(items[0].enclosure)["url"].endswith("movie.m3u8")


def test_resource_torrents_filters_by_requested_media_type(monkeypatch):
    class TorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    tv = CmsResult(
        source_key="demo",
        source_name="演示源",
        vod_id="tv",
        title="示例剧",
        year="2024",
        media_type="tv",
        remark="",
        episodes=(CmsEpisode(1, 1, "第1集", "https://video.example/tv.m3u8"),),
    )
    movie = CmsResult(
        source_key="demo",
        source_name="演示源",
        vod_id="movie",
        title="示例电影",
        year="2024",
        media_type="movie",
        remark="",
        episodes=(CmsEpisode(1, 1, "正片", "https://video.example/movie.m3u8"),),
    )

    calls = []

    class Client:
        def search(self, *_args, **kwargs):
            calls.append(kwargs)
            return [tv, movie]

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", TorrentInfo)
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_associate_tmdb", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        plugin, "_probe_resource_urls", lambda urls: {url: 720 for url in urls}
    )

    tv_items = plugin._resource_torrents("类型过滤", mtype="tv")
    movie_items = plugin._resource_torrents("类型过滤", mtype="movie")
    unknown_items = plugin._resource_torrents("类型过滤", mtype="unknown")

    assert [
        plugin._decode_resource_token(item.enclosure)["media_type"] for item in tv_items
    ] == ["tv"]
    assert [
        plugin._decode_resource_token(item.enclosure)["media_type"] for item in movie_items
    ] == ["movie"]
    assert sorted(
        plugin._decode_resource_token(item.enclosure)["media_type"]
        for item in unknown_items
    ) == ["movie", "tv"]
    assert calls[0]["media_type_filter"] == "tv"
    assert calls[1]["media_type_filter"] == "movie"
    assert "media_type_filter" not in calls[2]


@pytest.mark.parametrize(
    ("mtype", "first_media_type", "expected_media_type"),
    [
        ("欧美剧", "tv", "tv"),
        ("韩剧", "tv", "tv"),
        ("movie", "tv", "movie"),
        ("tv", "movie", "tv"),
    ],
)
def test_resource_search_context_uses_first_result_for_noncanonical_type(
    mtype: str,
    first_media_type: str,
    expected_media_type: str,
):
    first = CmsResult(
        source_key="demo",
        source_name="演示源",
        vod_id="42",
        title="示例作品",
        year="2024",
        media_type=first_media_type,
        remark="",
        episodes=(),
    )

    context = LunaTVSource._resource_search_context("示例作品", [first], mtype)

    assert context.media_type == expected_media_type


def test_resource_torrents_keep_complete_season_quality_variants_separate(monkeypatch):
    class TorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    source = CmsSource("demo", "演示源", "https://cms.example/vod")
    low = _result_from_item(
        source,
        {
            "vod_id": "low",
            "vod_name": "示例剧 第一季",
            "type_name": "电视剧",
            "vod_play_url": (
                "第1集$https://video.example/480-e1.m3u8#"
                "第2集$https://video.example/480-e2.m3u8"
            ),
        },
    )
    high = _result_from_item(
        source,
        {
            "vod_id": "high",
            "vod_name": "示例剧 第一季",
            "type_name": "电视剧",
            "vod_play_url": (
                "第1集$https://video.example/1080-e1.m3u8#"
                "第2集$https://video.example/1080-e2.m3u8"
            ),
        },
    )

    class Client:
        def search(self, *_args, **_kwargs):
            return [low, high]

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", TorrentInfo)
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_associate_tmdb", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        plugin,
        "_probe_resource_urls",
        lambda urls: {url: 1080 if "/1080-" in url else 480 for url in urls},
    )

    items = plugin._resource_torrents("示例剧")
    payloads = [plugin._decode_resource_token(item.enclosure) for item in items]

    assert [item.pri_order for item in items] == [108, 48]
    assert [payload["resolution"] for payload in payloads] == ["1080P", "480P"]
    assert [len(payload["episodes"]) for payload in payloads] == [2, 2]
    assert all("1080-" in item["url"] for item in payloads[0]["episodes"])
    assert all("480-" in item["url"] for item in payloads[1]["episodes"])
    assert all(
        len({episode["url"] for episode in payload["episodes"]}) == 2
        for payload in payloads
    )


def test_resource_torrents_choose_highest_url_for_conflicting_episode(monkeypatch):
    class TorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    source = CmsSource("demo", "演示源", "https://cms.example/vod")
    rows = [
        _result_from_item(
            source,
            {
                "vod_id": "e1-low",
                "vod_name": "示例剧 第一季 第1集",
                "type_name": "电视剧",
                "vod_play_url": "第1集$https://video.example/480-e1.m3u8",
            },
        ),
        _result_from_item(
            source,
            {
                "vod_id": "e1-high",
                "vod_name": "示例剧 第一季 第1集",
                "type_name": "电视剧",
                "vod_play_url": "第1集$https://video.example/1080-e1.m3u8",
            },
        ),
        _result_from_item(
            source,
            {
                "vod_id": "e2",
                "vod_name": "示例剧 第一季 第2集",
                "type_name": "电视剧",
                "vod_play_url": "第2集$https://video.example/480-e2.m3u8",
            },
        ),
    ]

    class Client:
        def search(self, *_args, **_kwargs):
            return rows

    probed = []

    def probe(urls):
        probed.extend(urls)
        return {url: 1080 if "/1080-" in url else 480 for url in urls}

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", TorrentInfo)
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_associate_tmdb", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(plugin, "_probe_resource_urls", probe)

    item = plugin._resource_torrents("示例剧")[0]
    payload = plugin._decode_resource_token(item.enclosure)

    assert item.title == "示例剧 · 第1季 · 480P"
    assert payload["resolution_height"] == 480
    assert item.pri_order == 48
    assert payload["resolution"] in item.description and payload["resolution"] in item.labels
    assert [episode["url"] for episode in payload["episodes"]] == [
        "https://video.example/1080-e1.m3u8",
        "https://video.example/480-e2.m3u8",
    ]
    assert "https://video.example/480-e1.m3u8" not in [
        episode["url"] for episode in payload["episodes"]
    ]
    assert [
        (episode["resolution"], episode["resolution_height"])
        for episode in payload["episodes"]
    ] == [("1080P", 1080), ("480P", 480)]
    assert payload["resolution_scope"] == "full"
    assert payload["resolution_probed_episode_count"] == 2
    assert payload["resolution_probed_episodes"] == [1, 2]
    assert "https://video.example/480-e2.m3u8" in probed


def test_resource_torrents_marks_season_unknown_when_any_episode_probe_fails(monkeypatch):
    class TorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    result = CmsResult(
        source_key="demo",
        source_name="演示源",
        vod_id="s1",
        title="示例剧",
        year="2024",
        media_type="tv",
        remark="",
        episodes=(
            CmsEpisode(1, 1, "第1集", "https://video.example/1080-e1.m3u8"),
            CmsEpisode(1, 2, "第2集", "https://video.example/unprobed-e2.m3u8"),
        ),
    )

    class Client:
        def search(self, *_args, **_kwargs):
            return [result]

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", TorrentInfo)
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_associate_tmdb", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        plugin,
        "_probe_resource_urls",
        lambda urls: {url: 1080 for url in urls if "1080" in url},
    )

    item = plugin._resource_torrents("示例剧")[0]
    payload = plugin._decode_resource_token(item.enclosure)

    assert payload["resolution"] == "未知"
    assert payload["resolution_height"] == 0
    assert item.pri_order == 0
    assert "未知" in item.title
    assert "已测1/2集" in item.description
    assert payload["resolution_scope"] == "partial"
    assert payload["resolution_probed_episode_count"] == 1
    assert payload["resolution_probed_episodes"] == [1]
    assert [
        (episode["resolution"], episode["resolution_height"])
        for episode in payload["episodes"]
    ] == [("1080P", 1080), ("未知", 0)]


def test_resource_torrents_probes_all_episodes_in_large_seasons(monkeypatch):
    class TorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Client:
        def __init__(self, result):
            self._result = result

        def search(self, *_args, **_kwargs):
            return [self._result]

    for count in (52, 208):
        result = CmsResult(
            source_key=f"sample-{count}",
            source_name="抽样源",
            vod_id=f"sample-{count}",
            title=f"抽样剧{count}",
            year="2024",
            media_type="tv",
            remark="",
            episodes=tuple(
                CmsEpisode(
                    1,
                    episode,
                    f"第{episode}集",
                    f"https://video.example/sample-{count}-e{episode}.m3u8",
                )
                for episode in range(1, count + 1)
            ),
        )
        probe_calls = []

        def probe(urls):
            probe_calls.append(list(urls))
            return {url: 1080 for url in urls}

        plugin = LunaTVSource()
        plugin.init_plugin({"enabled": True})
        monkeypatch.setattr(plugin_module, "_HostTorrentInfo", TorrentInfo)
        monkeypatch.setattr(plugin, "_client", lambda: Client(result))
        monkeypatch.setattr(plugin, "_associate_tmdb", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(plugin, "_probe_resource_urls", probe)

        item = plugin._resource_torrents(f"抽样剧{count}")[0]
        payload = plugin._decode_resource_token(item.enclosure)
        expected_episodes = list(range(1, count + 1))
        expected_urls = [
            f"https://video.example/sample-{count}-e{episode}.m3u8"
            for episode in expected_episodes
        ]

        assert [urls for urls in probe_calls if urls] == [expected_urls]
        assert len(expected_urls) == count
        assert item.pri_order == 108
        assert f"全{count}集实测" in item.description
        assert payload["resolution_scope"] == "full"
        assert payload["resolution_probed_episode_count"] == count
        assert payload["resolution_probed_episodes"] == expected_episodes
        assert [
            (payload["episodes"][episode - 1]["resolution"],
             payload["episodes"][episode - 1]["resolution_height"])
            for episode in expected_episodes
        ] == [("1080P", 1080)] * count


def test_resource_torrents_probes_all_conflicts_and_large_seasons(monkeypatch):
    class TorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    low_url = "https://video.example/conflict-low-e1.m3u8"
    high_url = "https://video.example/conflict-high-e1.m3u8"
    rows = [
        CmsResult(
            source_key="demo",
            source_name="演示源",
            vod_id="e1-low",
            title="冲突剧",
            year="2024",
            media_type="tv",
            remark="",
            episodes=(CmsEpisode(1, 1, "第1集", low_url),),
        ),
        CmsResult(
            source_key="demo",
            source_name="演示源",
            vod_id="e1-high",
            title="冲突剧",
            year="2024",
            media_type="tv",
            remark="",
            episodes=(CmsEpisode(1, 1, "第1集", high_url),),
        ),
    ] + [
        CmsResult(
            source_key="demo",
            source_name="演示源",
            vod_id=f"e{episode}",
            title="冲突剧",
            year="2024",
            media_type="tv",
            remark="",
            episodes=(
                CmsEpisode(
                    1,
                    episode,
                    f"第{episode}集",
                    f"https://video.example/conflict-e{episode}.m3u8",
                ),
            ),
        )
        for episode in range(2, 53)
    ]

    class Client:
        def search(self, *_args, **_kwargs):
            return rows

    probe_calls = []

    def probe(urls):
        probe_calls.append(list(urls))
        return {
            url: (
                1080
                if url == high_url
                else 480
                if url == low_url
                else 0
                if url.endswith("conflict-e27.m3u8")
                else 720
            )
            for url in urls
        }

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", TorrentInfo)
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_associate_tmdb", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(plugin, "_probe_resource_urls", probe)

    item = plugin._resource_torrents("冲突剧")[0]
    payload = plugin._decode_resource_token(item.enclosure)
    probed_urls = [url for urls in probe_calls for url in urls]

    assert probed_urls.count(low_url) == 1
    assert probed_urls.count(high_url) == 1
    assert payload["episodes"][0]["url"] == high_url
    assert payload["resolution"] == "未知"
    assert payload["resolution_height"] == item.pri_order == 0
    assert "未知" in item.title
    assert "已测51/52集" in item.description
    assert payload["resolution_scope"] == "partial"
    assert payload["resolution_probed_episode_count"] == 51
    assert payload["resolution_probed_episodes"] == [
        episode for episode in range(1, 53) if episode != 27
    ]
    assert (
        payload["episodes"][1]["resolution"],
        payload["episodes"][1]["resolution_height"],
    ) == ("720P", 720)
    assert (
        payload["episodes"][26]["resolution"],
        payload["episodes"][26]["resolution_height"],
    ) == ("未知", 0)


def test_resource_torrents_sort_actual_heights_and_keep_ties_stable(monkeypatch):
    class TorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    source = CmsSource("demo", "演示源", "https://cms.example/vod")
    qualities = [
        ("1080-first", 1080),
        ("2160", 2160),
        ("1440", 1440),
        ("1200", 1200),
        ("1080-second", 1080),
        ("720", 720),
        ("480", 480),
        ("unknown", 0),
    ]
    results = [
        _result_from_item(
            source,
            {
                "vod_id": name,
                "vod_name": f"质量 {name}",
                "type_name": "电影",
                "vod_play_url": f"正片$https://video.example/{name}.m3u8",
            },
        )
        for name, _ in qualities
    ]
    heights = {
        f"https://video.example/{name}.m3u8": height for name, height in qualities
    }

    class Client:
        def search(self, *_args, **_kwargs):
            return results

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", TorrentInfo)
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_associate_tmdb", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(plugin, "_probe_resource_urls", lambda urls: {url: heights[url] for url in urls})

    items = plugin._resource_torrents("质量")

    assert [item.pri_order for item in items] == [216, 144, 120, 108, 108, 72, 48, 0]
    host_sorted = sorted(
        items,
        key=lambda item: str(item.pri_order or 0).rjust(3, "0"),
        reverse=True,
    )
    assert [
        plugin._decode_resource_token(item.enclosure)["url"] for item in host_sorted
    ] == [
        plugin._decode_resource_token(item.enclosure)["url"] for item in items
    ]
    assert [plugin._decode_resource_token(item.enclosure)["url"] for item in items] == [
        "https://video.example/2160.m3u8",
        "https://video.example/1440.m3u8",
        "https://video.example/1200.m3u8",
        "https://video.example/1080-first.m3u8",
        "https://video.example/1080-second.m3u8",
        "https://video.example/720.m3u8",
        "https://video.example/480.m3u8",
        "https://video.example/unknown.m3u8",
    ]
    for item in items:
        payload = plugin._decode_resource_token(item.enclosure)
        quality = payload["resolution"]
        assert item.title.endswith(f"· {quality}")
        assert quality in item.description and quality in item.labels
        assert item.pri_order == plugin_module._resource_sort_priority(
            payload["resolution_height"]
        )


def test_subscription_candidates_prefer_verified_resolution(monkeypatch):
    low = _result_from_item(
        CmsSource("low", "标清源", "https://low.example/vod"),
        {
            "vod_id": "1",
            "vod_name": "示例剧",
            "type_name": "电视剧",
            "vod_play_url": "01$https://video.example/480.m3u8",
        },
    )
    high = _result_from_item(
        CmsSource("high", "高清源", "https://high.example/vod"),
        {
            "vod_id": "2",
            "vod_name": "示例剧",
            "type_name": "电视剧",
            "vod_play_url": "01$https://video.example/1080.m3u8",
        },
    )
    monkeypatch.setattr(
        plugin_module,
        "probe_stream_height",
        lambda url, **_kwargs: 1080 if "1080" in url else 480,
    )
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})

    ranked = plugin._rank_subscription_results([(low, {}), (high, {})], season=1)

    assert [result.source_name for result, _ in ranked] == ["高清源", "标清源"]


def test_resource_search_does_not_hold_cache_lock_during_network_request(monkeypatch):
    class TorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    lock_available = []

    class Client:
        def search(self, query, **kwargs):
            acquired = plugin._resource_search_lock.acquire(blocking=False)
            lock_available.append(acquired)
            if acquired:
                plugin._resource_search_lock.release()
            return []

    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", TorrentInfo)
    monkeypatch.setattr(plugin, "_client", lambda: Client())

    assert plugin.search_torrents(site={"id": 1}, keyword="示例剧", page=0) == []
    assert lock_available == [True]


def _install_search_chain_module(monkeypatch, search_chain):
    app_module = ModuleType("app")
    app_module.__path__ = []
    chain_module = ModuleType("app.chain")
    chain_module.__path__ = []
    search_module = ModuleType("app.chain.search")
    search_module.SearchChain = search_chain
    app_module.chain = chain_module
    chain_module.search = search_module
    monkeypatch.setitem(sys.modules, "app", app_module)
    monkeypatch.setitem(sys.modules, "app.chain", chain_module)
    monkeypatch.setitem(sys.modules, "app.chain.search", search_module)


def test_plugin_search_bridge_augments_legacy_search_and_restores(monkeypatch):
    import asyncio

    class SearchChain:
        def __search_all_sites(self, **_kwargs):
            return ["native-sync"]

        async def __async_search_all_sites(self, **_kwargs):
            return ["native-async"]

        async def __async_search_all_sites_stream(self, **_kwargs):
            yield {"type": "heartbeat", "items": [], "text": "native heartbeat"}
            yield {
                "type": "done",
                "stage": "searching",
                "items": [],
                "text": "native done",
            }

    _install_search_chain_module(monkeypatch, SearchChain)
    plugin_module._SEARCH_BRIDGE.update(
        {"owner": None, "chain": None, "originals": {}, "mode": None}
    )
    sync_original = SearchChain._SearchChain__search_all_sites
    async_original = SearchChain._SearchChain__async_search_all_sites
    stream_original = SearchChain._SearchChain__async_search_all_sites_stream
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin, "search_torrents", lambda **_kwargs: ["plugin-sync"])

    async def plugin_async_search(**_kwargs):
        return ["plugin-async"]

    monkeypatch.setattr(plugin, "async_search_torrents", plugin_async_search)
    try:
        chain = SearchChain()
        assert chain._SearchChain__search_all_sites(keyword="demo") == [
            "native-sync",
            "plugin-sync",
        ]
        assert asyncio.run(
            chain._SearchChain__async_search_all_sites(keyword="demo")
        ) == ["native-async", "plugin-async"]

        async def collect_stream():
            return [
                event
                async for event in chain._SearchChain__async_search_all_sites_stream(
                    keyword="demo"
                )
            ]

        events = asyncio.run(collect_stream())
        assert [event["type"] for event in events] == ["heartbeat", "append", "done"]
        assert events[1]["items"] == ["plugin-async"]
        assert events[1]["text"] == "LunaTV 返回 1 条资源"
        assert events[-1]["text"] == "资源搜索完成，LunaTV 返回 1 条资源"
    finally:
        plugin.init_plugin({"enabled": False})

    assert SearchChain._SearchChain__search_all_sites is sync_original
    assert SearchChain._SearchChain__async_search_all_sites is async_original
    assert SearchChain._SearchChain__async_search_all_sites_stream is stream_original


def test_async_search_torrents_uses_context_callback_unless_explicit(monkeypatch):
    import asyncio

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    callbacks = []
    context_callback = lambda **_event: None
    explicit_callback = lambda **_event: None

    def fake_search_torrents(**kwargs):
        callbacks.append(kwargs.get("progress_callback"))
        return []

    monkeypatch.setattr(plugin, "search_torrents", fake_search_torrents)

    async def run():
        token = plugin_module._SEARCH_PROGRESS_CALLBACK.set(context_callback)
        try:
            await plugin.async_search_torrents(site={}, keyword="context")
            await plugin.async_search_torrents(
                site={},
                keyword="explicit",
                progress_callback=explicit_callback,
            )
        finally:
            plugin_module._SEARCH_PROGRESS_CALLBACK.reset(token)

    asyncio.run(run())
    assert callbacks == [context_callback, explicit_callback]


def test_native_search_stream_progress_precedes_native_append_and_done(monkeypatch):
    import asyncio

    native_calls = []
    plugin_search_calls = []

    class SearchChain:
        def search_plugin_torrents(self, **_kwargs):
            return ["native-plugin-sync"]

        async def async_search_plugin_torrents(self, **kwargs):
            native_calls.append(kwargs["keyword"])
            return await plugin.async_search_torrents(
                site={},
                keyword=kwargs["keyword"],
                page=kwargs.get("page", 0),
            )

        def __search_all_sites(self, **_kwargs):
            return ["native-sync"]

        async def __async_search_all_sites(self, **_kwargs):
            return ["native-async"]

        async def __async_search_all_sites_stream(self, **kwargs):
            items = await self.async_search_plugin_torrents(**kwargs)
            yield {"type": "append", "items": items, "text": "native append"}
            yield {"type": "done", "items": [], "text": "native done"}

    _install_search_chain_module(monkeypatch, SearchChain)
    plugin_module._SEARCH_BRIDGE.update(
        {"owner": None, "chain": None, "originals": {}, "mode": None}
    )
    native_sync_original = SearchChain.search_plugin_torrents
    native_async_original = SearchChain.async_search_plugin_torrents
    sync_original = SearchChain._SearchChain__search_all_sites
    async_original = SearchChain._SearchChain__async_search_all_sites
    stream_original = SearchChain._SearchChain__async_search_all_sites_stream
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})

    def fake_search_torrents(**kwargs):
        plugin_search_calls.append(kwargs["keyword"])
        callback = kwargs["progress_callback"]
        callback(finished=1, total=2, text="LunaTV 正在搜索源 1/2")
        callback(finished=2, total=2, text="LunaTV 正在搜索源 2/2")
        return ["luna"]

    monkeypatch.setattr(plugin, "search_torrents", fake_search_torrents)
    try:
        wrapped_stream = SearchChain._SearchChain__async_search_all_sites_stream
        plugin.init_plugin({"enabled": True})
        assert SearchChain._SearchChain__async_search_all_sites_stream is wrapped_stream
        assert SearchChain.search_plugin_torrents is native_sync_original
        assert SearchChain.async_search_plugin_torrents is native_async_original
        assert SearchChain._SearchChain__search_all_sites is sync_original
        assert SearchChain._SearchChain__async_search_all_sites is async_original

        async def collect_stream():
            return [
                event
                async for event in SearchChain()._SearchChain__async_search_all_sites_stream(
                    keyword="demo",
                    page=3,
                )
            ]

        events = asyncio.run(collect_stream())
        assert native_calls == ["demo"]
        assert plugin_search_calls == ["demo"]
        assert [event["type"] for event in events] == [
            "progress",
            "progress",
            "append",
            "done",
        ]
        assert [
            (
                event["finished"],
                event["total"],
                event["value"],
                event["text"],
                event["stage"],
                event["items"],
                event["site"],
                event["site_id"],
                event["page"],
            )
            for event in events[:2]
        ] == [
            (1, 2, 50, "LunaTV 正在搜索源 1/2", "searching", [], "LunaTV", None, 3),
            (2, 2, 100, "LunaTV 正在搜索源 2/2", "searching", [], "LunaTV", None, 3),
        ]
        assert events[2] == {
            "type": "append",
            "items": ["luna"],
            "text": "native append",
        }
        assert events[3] == {"type": "done", "items": [], "text": "native done"}
    finally:
        plugin.init_plugin({"enabled": False})

    assert SearchChain.search_plugin_torrents is native_sync_original
    assert SearchChain.async_search_plugin_torrents is native_async_original
    assert SearchChain._SearchChain__search_all_sites is sync_original
    assert SearchChain._SearchChain__async_search_all_sites is async_original
    assert SearchChain._SearchChain__async_search_all_sites_stream is stream_original


def test_native_search_stream_progress_isolated_between_requests(monkeypatch):
    import asyncio

    rendezvous = threading.Barrier(2, timeout=1)
    plugin_search_calls = []

    class SearchChain:
        def search_plugin_torrents(self, **_kwargs):
            return []

        async def async_search_plugin_torrents(self, **kwargs):
            return await plugin.async_search_torrents(
                site={},
                keyword=kwargs["keyword"],
                page=kwargs.get("page", 0),
            )

        async def __async_search_all_sites_stream(self, **kwargs):
            items = await self.async_search_plugin_torrents(**kwargs)
            yield {"type": "append", "items": items, "text": kwargs["keyword"]}
            yield {"type": "done", "items": [], "text": kwargs["keyword"]}

    _install_search_chain_module(monkeypatch, SearchChain)
    plugin_module._SEARCH_BRIDGE.update(
        {"owner": None, "chain": None, "originals": {}, "mode": None}
    )
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})

    def fake_search_torrents(**kwargs):
        keyword = kwargs["keyword"]
        plugin_search_calls.append(keyword)
        callback = kwargs["progress_callback"]
        callback(finished=1, total=2, text=f"{keyword} 1/2")
        rendezvous.wait()
        callback(finished=2, total=2, text=f"{keyword} 2/2")
        return [keyword]

    monkeypatch.setattr(plugin, "search_torrents", fake_search_torrents)
    try:
        async def collect(keyword):
            return [
                event
                async for event in SearchChain()._SearchChain__async_search_all_sites_stream(
                    keyword=keyword
                )
            ]

        async def collect_both():
            return await asyncio.gather(collect("first"), collect("second"))

        first, second = asyncio.run(collect_both())
    finally:
        plugin.init_plugin({"enabled": False})

    assert sorted(plugin_search_calls) == ["first", "second"]
    for keyword, events in (("first", first), ("second", second)):
        assert [event["type"] for event in events] == [
            "progress",
            "progress",
            "append",
            "done",
        ]
        assert [event["text"] for event in events[:2]] == [
            f"{keyword} 1/2",
            f"{keyword} 2/2",
        ]
        assert events[2]["items"] == [keyword]


def test_native_search_stream_discards_late_progress_after_cancellation(monkeypatch):
    import asyncio
    from threading import Event

    slow_started = Event()
    release_slow = Event()
    slow_finished = Event()

    class SearchChain:
        def search_plugin_torrents(self, **_kwargs):
            return []

        async def async_search_plugin_torrents(self, **kwargs):
            return await plugin.async_search_torrents(
                site={},
                keyword=kwargs["keyword"],
            )

        async def __async_search_all_sites_stream(self, **kwargs):
            items = await self.async_search_plugin_torrents(**kwargs)
            yield {"type": "append", "items": items}
            yield {"type": "done", "items": []}

    _install_search_chain_module(monkeypatch, SearchChain)
    plugin_module._SEARCH_BRIDGE.update(
        {"owner": None, "chain": None, "originals": {}, "mode": None}
    )
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})

    def fake_search_torrents(**kwargs):
        callback = kwargs["progress_callback"]
        callback(finished=1, total=2, text="LunaTV 正在搜索源 1/2")
        slow_started.set()
        release_slow.wait(1)
        callback(finished=2, total=2, text="LunaTV 正在搜索源 2/2")
        slow_finished.set()
        return ["luna"]

    monkeypatch.setattr(plugin, "search_torrents", fake_search_torrents)
    try:
        async def collect_until_cancelled():
            events = []

            async def consume():
                async for event in SearchChain()._SearchChain__async_search_all_sites_stream(
                    keyword="demo"
                ):
                    events.append(event)

            consumer = asyncio.create_task(consume())
            for _ in range(100):
                if slow_started.is_set() and events:
                    break
                await asyncio.sleep(0.01)
            assert [event["type"] for event in events] == ["progress"]
            consumer.cancel()
            try:
                await consumer
            except asyncio.CancelledError:
                pass
            release_slow.set()
            assert await asyncio.to_thread(slow_finished.wait, 1)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return events

        events = asyncio.run(collect_until_cancelled())
    finally:
        release_slow.set()
        plugin.init_plugin({"enabled": False})

    assert [event["type"] for event in events] == ["progress"]


def test_native_download_is_enqueued_into_serial_queue(monkeypatch, tmp_path: Path):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    wakeups = []
    monkeypatch.setattr(plugin, "_start_queue", lambda: wakeups.append(True))
    token = plugin._resource_token({
        "url": "https://example.test/movie.m3u8",
        "title": "示例电影",
        "year": "2024",
        "media_type": "movie",
        "season": 1,
        "episode": 1,
        "media_id": "demo:42",
        "host_media_source": "themoviedb",
        "host_media_id": "123",
    })
    result = plugin.download(token, tmp_path)
    assert result[0] == "LunaTVSource"
    assert result[1]
    tasks = plugin._queue.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["url"] == "https://example.test/movie.m3u8"
    assert tasks[0]["root"] == str(tmp_path)
    assert tasks[0]["source_key"] == "demo"
    assert tasks[0]["media_id"] == "demo:42"
    assert tasks[0]["host_media_source"] == "themoviedb"
    assert tasks[0]["host_media_id"] == "123"
    assert wakeups == [True]


def test_native_season_download_expands_to_serial_episode_tasks(monkeypatch, tmp_path: Path):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    wakeups = []
    monkeypatch.setattr(plugin, "_start_queue", lambda: wakeups.append(True))
    token = plugin._resource_token({
        "url": "https://example.test/s01e01.m3u8",
        "title": "小猪佩奇",
        "year": "2004",
        "media_type": "tv",
        "season": 1,
        "episode": 1,
        "media_id": "demo:42",
        "source_key": "demo",
        "source_name": "演示源",
        "host_media_source": "themoviedb",
        "host_media_id": "123",
        "episodes": [
            {
                "url": "https://example.test/s01e01.m3u8",
                "title": "小猪佩奇",
                "year": "2004",
                "media_type": "tv",
                "season": 1,
                "episode": 1,
                "source_key": "demo",
                "source_name": "演示源",
                "media_id": "demo:42",
                "host_media_source": "themoviedb",
                "host_media_id": "123",
            },
            {
                "url": "https://example.test/s01e02.m3u8",
                "title": "小猪佩奇",
                "year": "2004",
                "media_type": "tv",
                "season": 1,
                "episode": 2,
                "source_key": "demo",
                "source_name": "演示源",
                "media_id": "demo:42",
                "host_media_source": "themoviedb",
                "host_media_id": "123",
            },
        ],
    })

    result = plugin.download(token, tmp_path)
    assert result[0] == "LunaTVSource"
    assert result[1]
    assert "已排队 2 集" in result[3]
    tasks = sorted(plugin._queue.list_tasks(), key=lambda task: (task["season"], task["episode"]))
    assert [(task["season"], task["episode"], task["url"]) for task in tasks] == [
        (1, 1, "https://example.test/s01e01.m3u8"),
        (1, 2, "https://example.test/s01e02.m3u8"),
    ]
    assert wakeups == [True]
    assert plugin.download(token, tmp_path)[1] is None
    assert wakeups == [True]


def test_native_download_uses_explicit_episodes_without_valid_top_level_url(
    monkeypatch, tmp_path: Path
):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    wakeups = []
    monkeypatch.setattr(plugin, "_start_queue", lambda: wakeups.append(True))
    token = plugin._resource_token(
        {
            "url": "file:///tmp/not-a-stream.m3u8",
            "title": "示例剧",
            "media_type": "tv",
        }
    )

    result = plugin.download(
        token,
        tmp_path,
        episodes=[
            None,
            {"url": "file:///tmp/not-an-http-stream.m3u8", "season": 1, "episode": 1},
            {"url": "https://example.test/s01e02.m3u8", "season": 1, "episode": 2},
        ],
    )

    assert result[1]
    assert "2 集参数无效" in result[3]
    assert [task["url"] for task in plugin._queue.list_tasks()] == [
        "https://example.test/s01e02.m3u8"
    ]
    assert wakeups == [True]


def test_native_download_empty_episodes_fall_back_to_top_level_url(
    monkeypatch, tmp_path: Path
):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    wakeups = []
    monkeypatch.setattr(plugin, "_start_queue", lambda: wakeups.append(True))
    token = plugin._resource_token(
        {
            "url": "https://example.test/movie.m3u8",
            "title": "示例电影",
            "media_type": "movie",
            "episodes": [
                {
                    "url": "https://example.test/season.m3u8",
                    "season": 1,
                    "episode": 2,
                }
            ],
        }
    )

    result = plugin.download(token, tmp_path, episodes=[])

    assert result[1]
    assert [task["url"] for task in plugin._queue.list_tasks()] == [
        "https://example.test/movie.m3u8"
    ]
    assert wakeups == [True]


def test_native_download_reports_duplicate_instead_of_fake_success(monkeypatch, tmp_path: Path):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin, "_start_queue", lambda: None)
    token = plugin._resource_token({
        "url": "https://example.test/movie.m3u8",
        "title": "示例电影",
        "year": "2024",
        "media_type": "movie",
        "season": 1,
        "episode": 1,
        "media_id": "demo:42",
    })

    first = plugin.download(token, tmp_path)
    duplicate = plugin.download(token, tmp_path)

    assert first[1]
    assert duplicate[:3] == ("LunaTVSource", None, None)
    assert "已在" in duplicate[3]
    assert len(plugin._queue.list_tasks()) == 1


def test_active_queue_tasks_project_to_native_download_list_and_filter(monkeypatch):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin, "_start_queue", lambda: True)
    pending = DownloadTask(
        task_id="pending-task",
        source_key="cms-demo",
        media_id="cms-demo:42",
        title="排队电视剧",
        year="2026",
        media_type="tv",
        season=2,
        episode=3,
        url="https://example.test/pending.m3u8",
        root="/downloads/tv",
        host_media_source="themoviedb",
        host_media_id="123",
        state="pending",
    )
    running = DownloadTask(
        task_id="running-task",
        source_key="cms-demo",
        media_id="cms-demo:43",
        title="下载中电影",
        year="2025",
        media_type="movie",
        season=1,
        episode=1,
        url="https://example.test/running.m3u8",
        root="/downloads/movie",
        state="running",
        progress=0.42,
    )
    completed = DownloadTask(
        task_id="completed-task",
        source_key="cms-demo",
        media_id="cms-demo:44",
        title="已完成电影",
        year="2024",
        media_type="movie",
        season=1,
        episode=1,
        url="https://example.test/completed.m3u8",
        root="/downloads/movie",
        state="completed",
    )
    paused = DownloadTask(
        task_id="paused-task",
        source_key="cms-demo",
        media_id="cms-demo:45",
        title="已暂停电影",
        year="2024",
        media_type="movie",
        season=1,
        episode=1,
        url="https://example.test/paused.m3u8",
        root="/downloads/movie",
        state="paused",
    )
    plugin.save_data(plugin._queue.DATA_KEY, [
        pending.to_dict(),
        running.to_dict(),
        completed.to_dict(),
        paused.to_dict(),
    ])

    module = plugin.get_module()
    assert "list_torrents" in module
    torrents = module["list_torrents"](status=SimpleNamespace(value="下载中"))
    assert [torrent.hash for torrent in torrents] == ["paused-task", "running-task", "pending-task"]
    assert all(torrent.downloader == "LunaTVSource" for torrent in torrents)
    assert next(torrent for torrent in torrents if torrent.hash == "paused-task").state == "paused"
    assert all(torrent.state == "downloading" for torrent in torrents if torrent.hash != "paused-task")
    assert next(torrent for torrent in torrents if torrent.hash == "running-task").progress == 42.0
    pending_torrent = next(torrent for torrent in torrents if torrent.hash == "pending-task")
    assert pending_torrent.progress == 0.0
    assert pending_torrent.title == "排队电视剧"
    assert pending_torrent.name == "排队电视剧"
    assert pending_torrent.save_path == "/downloads/tv"
    assert pending_torrent.season_episode == "S02E03"
    assert _field(pending_torrent.media, "media_source") == "themoviedb"
    assert _field(pending_torrent.media, "media_id") == "123"

    assert sorted(torrent.hash for torrent in plugin.list_torrents(downloader="qBittorrent")) == [
        "paused-task",
        "pending-task",
        "running-task",
    ]
    assert sorted(torrent.hash for torrent in plugin.list_torrents(downloader="下载器1")) == [
        "paused-task",
        "pending-task",
        "running-task",
    ]
    assert sorted(torrent.hash for torrent in plugin.list_torrents(downloader="我的自定义客户端")) == [
        "paused-task",
        "pending-task",
        "running-task",
    ]
    assert plugin.list_torrents(status="completed") == []
    assert plugin.list_torrents(status="transfer") == []
    assert [torrent.hash for torrent in plugin.list_torrents(
        downloader="LunaTVSource", hashs=["pending-task"]
    )] == ["pending-task"]
    paused_torrents = plugin.list_torrents(status="paused")
    assert [torrent.hash for torrent in paused_torrents] == ["paused-task"]
    assert paused_torrents[0].state == "paused"
    assert module["start_torrents"](["paused-task"], downloader="下载器1") is True
    assert next(item for item in plugin._queue.list_tasks() if item["task_id"] == "paused-task")["state"] == "pending"
    assert next(
        torrent
        for torrent in plugin.list_torrents(status="downloading")
        if torrent.hash == "paused-task"
    ).state == "downloading"

    assert {"start_torrents", "stop_torrents", "remove_torrents"} <= module.keys()
    assert module["stop_torrents"](["pending-task"], downloader="下载器1") is True
    assert next(item for item in plugin._queue.list_tasks() if item["task_id"] == "pending-task")["state"] == "paused"
    assert module["start_torrents"](["pending-task"], downloader="下载器1") is True
    assert next(item for item in plugin._queue.list_tasks() if item["task_id"] == "pending-task")["state"] == "pending"
    assert module["remove_torrents"](
        ["pending-task"], delete_file=True, downloader="下载器1"
    ) is True
    assert all(item["task_id"] != "pending-task" for item in plugin._queue.list_tasks())
    assert module["stop_torrents"](["native-qbt-hash"], downloader="下载器1") is None


def test_native_resume_wakes_serial_queue(monkeypatch, tmp_path: Path):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    task = DownloadTask(
        task_id="paused-native-task",
        source_key="cms-demo",
        media_id="cms-demo:46",
        title="继续下载电影",
        year="2026",
        media_type="movie",
        season=1,
        episode=1,
        url="https://example.test/resume.m3u8",
        root=str(tmp_path),
        state="paused",
    )
    plugin.save_data(plugin._queue.DATA_KEY, [task.to_dict()])
    started = threading.Event()

    def run_one():
        started.set()
        return {"processed": 1}

    monkeypatch.setattr(plugin._queue, "run_one", run_one)

    assert plugin.start_torrents([task.task_id], downloader="下载器1") is True
    assert started.wait(timeout=1)
    assert plugin._queue.list_tasks()[0]["state"] == "pending"


def test_active_queue_projection_uses_host_downloader_torrent_when_available(monkeypatch):
    class HostDownloaderTorrent:
        def __init__(self, **values):
            self.__dict__.update(values)

    monkeypatch.setattr(
        plugin_module,
        "_schemas",
        SimpleNamespace(DownloaderTorrent=HostDownloaderTorrent),
    )
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    task = DownloadTask(
        task_id="host-torrent-task",
        source_key="cms-demo",
        media_id="cms-demo:45",
        title="宿主投影",
        year="2026",
        media_type="movie",
        season=1,
        episode=1,
        url="https://example.test/host.m3u8",
        root="/downloads/movie",
    )
    plugin.save_data(plugin._queue.DATA_KEY, [task.to_dict()])

    torrents = plugin.list_torrents()
    assert len(torrents) == 1
    assert isinstance(torrents[0], HostDownloaderTorrent)


def test_active_queue_projection_reports_partial_size_and_speed(monkeypatch, tmp_path: Path):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    task = DownloadTask(
        task_id="metrics-task",
        source_key="cms-demo",
        media_id="cms-demo:47",
        title="进度电影",
        year="2026",
        media_type="movie",
        season=1,
        episode=1,
        url="https://example.test/metrics.m3u8",
        root=str(tmp_path),
        state="running",
        progress=0.5,
    )
    relative_dir, filename = media_path(
        task.root,
        task.title,
        task.year,
        task.media_type,
        task.season,
        task.episode,
        task.url,
        task.mode,
    )
    partial = tmp_path / relative_dir / f"{filename}.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"x" * 1024)
    timestamps = iter([100.0, 102.0])
    monkeypatch.setattr(plugin_module.time, "monotonic", lambda: next(timestamps))

    first = plugin._active_download_torrent(task)
    partial.write_bytes(b"x" * 3072)
    second = plugin._active_download_torrent(task)

    assert first.size == 2048.0
    assert first.dlspeed == "0.0B"
    assert second.size == 6144.0
    assert second.dlspeed == "1.0K"


def test_active_queue_projection_clamps_fractional_progress_to_percent():
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    task = DownloadTask(
        task_id="overreported-progress",
        source_key="cms-demo",
        media_id="cms-demo:46",
        title="进度边界",
        year="2026",
        media_type="movie",
        season=1,
        episode=1,
        url="https://example.test/progress.m3u8",
        root="/downloads/movie",
        state="running",
        progress=1.2,
    )

    assert plugin._active_download_torrent(task).progress == 100.0
    task.progress = -0.1
    assert plugin._active_download_torrent(task).progress == 0.0


def test_resource_download_event_allows_native_chain_to_call_plugin_download(tmp_path: Path):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    token = plugin._resource_token({
        "url": "https://example.test/event.m3u8",
        "title": "事件电影",
        "year": "2024",
        "media_type": "movie",
        "season": 1,
        "episode": 1,
        "media_id": "demo:99",
        "host_media_source": "themoviedb",
        "host_media_id": "999",
    })
    event_data = SimpleNamespace(
        context=SimpleNamespace(torrent_info=SimpleNamespace(enclosure=token)),
        options={"save_path": f"local:{tmp_path}"},
        cancel=False,
        source="",
        reason="",
    )

    plugin._on_resource_download(SimpleNamespace(event_data=event_data))

    assert event_data.cancel is False
    assert event_data.source == "LunaTVSource-原生下载模块"
    assert plugin._queue.list_tasks() == []


def test_native_transfer_uses_host_identity(monkeypatch, tmp_path: Path):
    captured = {}

    class MediaSource(str, Enum):
        TMDB = "themoviedb"

    class StorageChain:
        def get_file_item(self, **kwargs):
            return object()

    class TransferChain:
        def manual_transfer(self, **kwargs):
            captured.update(kwargs)
            return True, ""

    monkeypatch.setattr(plugin_module, "_HostMediaSource", MediaSource)
    monkeypatch.setattr(plugin_module, "_HostStorageChain", StorageChain)
    monkeypatch.setattr(plugin_module, "_HostTransferChain", TransferChain)
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    task = SimpleNamespace(
        mode="download",
        media_type="movie",
        root=str(tmp_path),
        source_key="cms-demo",
        media_id="cms-demo:42",
        host_media_source="themoviedb",
        host_media_id="1084242",
        season=1,
    )

    assert plugin._native_transfer(task, str(tmp_path / "movie.mp4")) == "moviepilot"
    assert captured["media_source"] is MediaSource.TMDB
    assert captured["media_id"] == "1084242"


def test_task_media_identity_prefers_host_fields():
    task = SimpleNamespace(
        source_key="lunatv",
        media_id="not-used",
        host_media_source="themoviedb",
        host_media_id="98765",
    )
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})

    source, media_id = plugin._task_media_identity(task)
    assert source == "themoviedb"
    assert media_id == "98765"


def test_record_native_history_uses_source_output_and_is_idempotent(monkeypatch, tmp_path: Path):
    histories: list[dict] = []
    files: list[dict] = []
    transfer_calls: list[dict] = []
    download_module = ModuleType("app.db.oper.downloadhistory")

    class FakeDownloadHistoryOper:
        def get_by_hash(self, download_hash):
            return next(
                (item for item in histories if item["download_hash"] == download_hash),
                None,
            )

        def get_files_by_hash(self, download_hash, state=None):
            return [
                item
                for item in files
                if item["download_hash"] == download_hash
                and (state is None or item["state"] == state)
            ]

        def get_file_by_fullpath(self, fullpath):
            return next(
                (item for item in reversed(files) if item["fullpath"] == fullpath),
                None,
            )

        def add(self, **kwargs):
            histories.append(kwargs)

        def add_files(self, items):
            files.extend(items)

    download_module.DownloadHistoryOper = FakeDownloadHistoryOper

    transfer_module = ModuleType("app.db.oper.transferhistory")

    class FakeTransferHistoryOper:
        def add(self, **kwargs):
            transfer_calls.append(kwargs)

    transfer_module.TransferHistoryOper = FakeTransferHistoryOper

    app_module = ModuleType("app")
    app_module.__path__ = []
    app_db_module = ModuleType("app.db")
    app_db_oper_module = ModuleType("app.db.oper")
    app_db_module.__path__ = []
    app_db_oper_module.__path__ = []
    app_module.db = app_db_module
    app_db_module.oper = app_db_oper_module
    app_db_oper_module.transferhistory = transfer_module
    app_db_oper_module.downloadhistory = download_module

    monkeypatch.setitem(sys.modules, "app", app_module)
    monkeypatch.setitem(sys.modules, "app.db", app_db_module)
    monkeypatch.setitem(sys.modules, "app.db.oper", app_db_oper_module)
    monkeypatch.setitem(sys.modules, "app.db.oper.transferhistory", transfer_module)
    monkeypatch.setitem(sys.modules, "app.db.oper.downloadhistory", download_module)

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})

    output = str(tmp_path / "movie.mp4")
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("x")

    task = SimpleNamespace(
        task_id="tid-01",
        source_key="cms-demo",
        source_name="演示源",
        media_id="cms-demo:42",
        host_media_source="themoviedb",
        host_media_id="tt112233",
        media_type="movie",
        title="测试电影",
        year="2026",
        season=1,
        episode=1,
        mode="download",
    )
    plugin._record_native_history(task, output)
    plugin._record_native_history(task, output)

    expected_hash = hashlib.sha1(f"{task.task_id}|{output}".encode("utf-8")).hexdigest()
    assert len(histories) == 1
    assert histories[0]["media_source"] == "themoviedb"
    assert histories[0]["media_id"] == "tt112233"
    assert histories[0]["path"] == output
    assert histories[0]["torrent_site"] == "演示源"
    assert files == [{
        "download_hash": expected_hash,
        "downloader": "LunaTVSource",
        "fullpath": output,
        "savepath": str(tmp_path),
        "filepath": "movie.mp4",
        "torrentname": "测试电影",
        "state": 1,
    }]
    assert transfer_calls == []


def test_refresh_reconciles_existing_episode_without_enqueue_or_transfer(monkeypatch, tmp_path: Path):
    """An older direct-write artifact must appear in native subscription history."""
    histories: list[dict] = []
    files: list[dict] = []
    result = _result_from_item(
        CmsSource("cms-demo", "演示源", "https://cms.example/vod"),
        {
            "vod_id": "42",
            "vod_name": "疯狂动物城2",
            "vod_year": "2025",
            "type_name": "电影",
            "vod_play_url": "正片$https://example.test/zootopia-2.m3u8",
        },
    )
    episode = result.episodes[0]
    relative_dir, filename = media_path(
        str(tmp_path),
        result.title,
        result.year,
        result.media_type,
        episode.season,
        episode.episode,
        episode.url,
        "download",
    )
    output = tmp_path / relative_dir / filename
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"completed movie")

    download_module = ModuleType("app.db.oper.downloadhistory")

    class FakeDownloadHistoryOper:
        def get_by_hash(self, download_hash):
            return next(
                (item for item in histories if item["download_hash"] == download_hash),
                None,
            )

        def get_files_by_hash(self, download_hash, state=None):
            return [
                item
                for item in files
                if item["download_hash"] == download_hash
                and (state is None or item["state"] == state)
            ]

        def get_file_by_fullpath(self, fullpath):
            return next(
                (item for item in reversed(files) if item["fullpath"] == fullpath),
                None,
            )

        def add(self, **kwargs):
            histories.append(kwargs)

        def add_files(self, items):
            files.extend(items)

    subscribe = SimpleNamespace(
        state="R",
        name="疯狂动物城2",
        year="2025",
        type="电影",
        season=0,
        media_source="themoviedb",
        media_id="1084242",
        save_path=str(tmp_path),
    )
    subscribe_module = ModuleType("app.db.oper.subscribe")

    class FakeSubscribeOper:
        def list(self, state=None):
            assert state in {None, "R,P"}
            return [subscribe]

    subscribe_module.SubscribeOper = FakeSubscribeOper
    download_module.DownloadHistoryOper = FakeDownloadHistoryOper
    app_module = ModuleType("app")
    app_module.__path__ = []
    app_db_module = ModuleType("app.db")
    app_db_module.__path__ = []
    app_db_oper_module = ModuleType("app.db.oper")
    app_db_oper_module.__path__ = []
    app_module.db = app_db_module
    app_db_module.oper = app_db_oper_module
    app_db_oper_module.subscribe = subscribe_module
    app_db_oper_module.downloadhistory = download_module
    monkeypatch.setitem(sys.modules, "app", app_module)
    monkeypatch.setitem(sys.modules, "app.db", app_db_module)
    monkeypatch.setitem(sys.modules, "app.db.oper", app_db_oper_module)
    monkeypatch.setitem(sys.modules, "app.db.oper.subscribe", subscribe_module)
    monkeypatch.setitem(sys.modules, "app.db.oper.downloadhistory", download_module)

    class Client:
        def search(self, query, **_kwargs):
            assert query == "疯狂动物城2"
            return [result]

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "download_root": str(tmp_path)})
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_prepare_result", lambda item: (item, {}))
    monkeypatch.setattr(plugin._ai, "normalize", lambda *_args: ("疯狂动物城2", False))
    monkeypatch.setattr(
        plugin,
        "_native_transfer",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not transfer existing file")),
    )

    first = plugin.refresh_subscriptions()
    second = plugin.refresh_subscriptions()

    assert first["queued"] == 0
    assert first["reconciled"] == 1
    assert second["queued"] == 0
    assert second["reconciled"] == 1
    assert plugin._queue is not None
    assert plugin._queue.summary()["pending"] == 0
    assert len(histories) == 1
    assert histories[0]["media_source"] == "themoviedb"
    assert histories[0]["media_id"] == "1084242"
    assert histories[0]["path"] == str(output)
    assert histories[0]["torrent_site"] == "演示源"
    assert len(files) == 1
    assert files[0]["fullpath"] == str(output)


def test_refresh_plugin_subscription_reuses_tmdb_identity_for_organize(monkeypatch, tmp_path: Path):
    result = _result_from_item(
        CmsSource("cms-demo", "演示源", "https://cms.example/vod"),
        {
            "vod_id": "42",
            "vod_name": "示例剧",
            "vod_year": "2026",
            "type_name": "电视剧",
            "vod_play_url": "S01E01$https://example.test/s01e01.m3u8",
        },
    )
    subscribe = SimpleNamespace(
        state="R",
        name="示例剧",
        year="2026",
        type="电视剧",
        season=1,
        media_source="lunatv",
        media_id="cms-demo:42",
        save_path=str(tmp_path),
    )
    subscribe_module = ModuleType("app.db.oper.subscribe")

    class FakeSubscribeOper:
        def list(self, state=None):
            return [subscribe]

    subscribe_module.SubscribeOper = FakeSubscribeOper
    app_module = ModuleType("app")
    app_module.__path__ = []
    app_db_module = ModuleType("app.db")
    app_db_module.__path__ = []
    app_db_oper_module = ModuleType("app.db.oper")
    app_db_oper_module.__path__ = []
    app_module.db = app_db_module
    app_db_module.oper = app_db_oper_module
    app_db_oper_module.subscribe = subscribe_module
    monkeypatch.setitem(sys.modules, "app", app_module)
    monkeypatch.setitem(sys.modules, "app.db", app_db_module)
    monkeypatch.setitem(sys.modules, "app.db.oper", app_db_oper_module)
    monkeypatch.setitem(sys.modules, "app.db.oper.subscribe", subscribe_module)

    class Client:
        def search(self, _query, **_kwargs):
            return [result]

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "download_root": str(tmp_path)})
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(
        plugin,
        "_prepare_result",
        lambda item: (item, {
            "status": "matched",
            "media_source": "themoviedb",
            "media_id": "999",
            "title": "TMDB 示例剧",
        }),
    )

    response = plugin.refresh_subscriptions()
    assert response["queued"] == 1
    task = plugin._queue.list_tasks()[0]
    assert task["title"] == "TMDB 示例剧"
    assert task["host_media_source"] == "themoviedb"
    assert task["host_media_id"] == "999"
    assert task["root"] == str(tmp_path)


def test_refresh_plugin_season_subscription_researches_and_queues_whole_season(
    monkeypatch, tmp_path: Path
):
    source = CmsSource("cms-demo", "演示源", "https://cms.example/vod")
    rows = [
        _result_from_item(
            source,
            {
                "vod_id": "episode-1",
                "vod_name": "示例剧 第一季 第1集",
                "vod_year": "2026",
                "type_name": "电视剧",
                "vod_play_url": "第1集$https://example.test/s01e01.m3u8",
            },
        ),
        _result_from_item(
            source,
            {
                "vod_id": "episode-2",
                "vod_name": "示例剧 第一季 第2集",
                "vod_year": "2026",
                "type_name": "电视剧",
                "vod_play_url": "第2集$https://example.test/s01e02.m3u8",
            },
        ),
    ]
    subscribe = SimpleNamespace(
        state="R",
        name="示例剧",
        year="2026",
        type="电视剧",
        season=1,
        media_source="lunatv",
        media_id="cms-demo:episode-1",
        save_path=str(tmp_path),
    )
    subscribe_module = ModuleType("app.db.oper.subscribe")

    class FakeSubscribeOper:
        def list(self, state=None):
            assert state == "R,P"
            return [subscribe]

    subscribe_module.SubscribeOper = FakeSubscribeOper
    app_module = ModuleType("app")
    app_module.__path__ = []
    app_db_module = ModuleType("app.db")
    app_db_module.__path__ = []
    app_db_oper_module = ModuleType("app.db.oper")
    app_db_oper_module.__path__ = []
    app_module.db = app_db_module
    app_db_module.oper = app_db_oper_module
    app_db_oper_module.subscribe = subscribe_module
    monkeypatch.setitem(sys.modules, "app", app_module)
    monkeypatch.setitem(sys.modules, "app.db", app_db_module)
    monkeypatch.setitem(sys.modules, "app.db.oper", app_db_oper_module)
    monkeypatch.setitem(sys.modules, "app.db.oper.subscribe", subscribe_module)

    searches = []

    class Client:
        def detail(self, *_args):
            raise AssertionError("season subscription must re-search, not detail one episode")

        def search(self, query, **_kwargs):
            searches.append(query)
            return rows

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "download_root": str(tmp_path)})
    wakeups = []
    monkeypatch.setattr(plugin, "_start_queue", lambda: wakeups.append(True))
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_prepare_result", lambda result: (result, {}))

    response = plugin.refresh_subscriptions()
    tasks = sorted(plugin._queue.list_tasks(), key=lambda task: task["episode"])

    assert searches
    assert response["queued"] == 2
    assert [(task["season"], task["episode"]) for task in tasks] == [(1, 1), (1, 2)]
    assert wakeups == [True]
    assert plugin.refresh_subscriptions()["queued"] == 0
    assert wakeups == [True]


def test_refresh_plugin_season_subscription_expands_52_cms_episode_rows(
    monkeypatch, tmp_path: Path
):
    source = CmsSource("cms-demo", "演示源", "https://cms.example/vod")
    client = AppleCmsClient([source])
    pages = []

    def fake_request(_source, **params):
        assert params.get("ac") == "list"
        page = int(params["pg"])
        pages.append(page)
        first = (page - 1) * 20 + 1
        last = min(first + 20, 53)
        return {
            "pagecount": "3",
            "list": [
                {
                    "vod_id": f"episode-{episode}",
                    "vod_name": f"示例剧 S01E{episode:03d}",
                    "vod_year": "2026",
                    "type_name": "电视剧",
                    "vod_play_url": (
                        f"第{episode}集$https://example.test/s01e{episode:03d}.m3u8"
                    ),
                }
                for episode in range(first, last)
            ],
        }

    client._request = fake_request
    subscribe = SimpleNamespace(
        state="R",
        name="示例剧",
        year="2026",
        type="电视剧",
        season=1,
        media_source="lunatv",
        media_id="cms-demo:episode-1",
        save_path=str(tmp_path),
    )
    subscribe_module = ModuleType("app.db.oper.subscribe")

    class FakeSubscribeOper:
        def list(self, state=None):
            assert state == "R,P"
            return [subscribe]

    subscribe_module.SubscribeOper = FakeSubscribeOper
    app_module = ModuleType("app")
    app_module.__path__ = []
    app_db_module = ModuleType("app.db")
    app_db_module.__path__ = []
    app_db_oper_module = ModuleType("app.db.oper")
    app_db_oper_module.__path__ = []
    app_module.db = app_db_module
    app_db_module.oper = app_db_oper_module
    app_db_oper_module.subscribe = subscribe_module
    monkeypatch.setitem(sys.modules, "app", app_module)
    monkeypatch.setitem(sys.modules, "app.db", app_db_module)
    monkeypatch.setitem(sys.modules, "app.db.oper", app_db_oper_module)
    monkeypatch.setitem(sys.modules, "app.db.oper.subscribe", subscribe_module)

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "download_root": str(tmp_path)})
    monkeypatch.setattr(plugin, "_start_queue", lambda: None)
    monkeypatch.setattr(plugin, "_client", lambda: client)
    monkeypatch.setattr(plugin, "_prepare_result", lambda result: (result, {}))

    response = plugin.refresh_subscriptions()
    tasks = sorted(plugin._queue.list_tasks(), key=lambda task: task["episode"])

    assert pages == [1, 2, 3]
    assert response["queued"] == 52
    assert [(task["season"], task["episode"]) for task in tasks] == [
        (1, episode) for episode in range(1, 53)
    ]


def test_refresh_plugin_season_subscription_queues_highest_resolution_for_same_episode(
    monkeypatch, tmp_path: Path
):
    source = CmsSource("cms-demo", "演示源", "https://cms.example/vod")
    rows = [
        _result_from_item(
            source,
            {
                "vod_id": "episode-1-low",
                "vod_name": "示例剧 第一季 第1集",
                "vod_year": "2026",
                "type_name": "电视剧",
                "vod_play_url": "第1集$https://example.test/480-s01e01.m3u8",
            },
        ),
        _result_from_item(
            source,
            {
                "vod_id": "episode-1-high",
                "vod_name": "示例剧 第一季 第1集",
                "vod_year": "2026",
                "type_name": "电视剧",
                "vod_play_url": "第1集$https://example.test/1080-s01e01.m3u8",
            },
        ),
    ]
    subscribe = SimpleNamespace(
        state="R",
        name="示例剧",
        year="2026",
        type="电视剧",
        season=1,
        media_source="lunatv",
        media_id="cms-demo:episode-1-low",
        save_path=str(tmp_path),
    )
    subscribe_module = ModuleType("app.db.oper.subscribe")

    class FakeSubscribeOper:
        def list(self, state=None):
            assert state == "R,P"
            return [subscribe]

    subscribe_module.SubscribeOper = FakeSubscribeOper
    app_module = ModuleType("app")
    app_module.__path__ = []
    app_db_module = ModuleType("app.db")
    app_db_module.__path__ = []
    app_db_oper_module = ModuleType("app.db.oper")
    app_db_oper_module.__path__ = []
    app_module.db = app_db_module
    app_db_module.oper = app_db_oper_module
    app_db_oper_module.subscribe = subscribe_module
    monkeypatch.setitem(sys.modules, "app", app_module)
    monkeypatch.setitem(sys.modules, "app.db", app_db_module)
    monkeypatch.setitem(sys.modules, "app.db.oper", app_db_oper_module)
    monkeypatch.setitem(sys.modules, "app.db.oper.subscribe", subscribe_module)

    class Client:
        def search(self, _query, **_kwargs):
            return rows

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "download_root": str(tmp_path)})
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_prepare_result", lambda result: (result, {}))
    monkeypatch.setattr(
        plugin,
        "_probe_resource_urls",
        lambda urls: {url: 1080 if "1080" in url else 480 for url in urls},
    )

    response = plugin.refresh_subscriptions()
    tasks = plugin._queue.list_tasks()

    assert response["queued"] == 1
    assert len(tasks) == 1
    assert tasks[0]["url"] == "https://example.test/1080-s01e01.m3u8"


def test_refresh_plugin_season_subscription_keeps_all_sources_seasons_separate(
    monkeypatch, tmp_path: Path
):
    source_a = CmsSource("cms-a", "源A", "https://a.example/vod")
    source_b = CmsSource("cms-b", "源B", "https://b.example/vod")
    rows = [
        _result_from_item(
            source_a,
            {
                "vod_id": "a-e1",
                "vod_name": "示例剧 第一季 第1集",
                "vod_year": "2026",
                "type_name": "电视剧",
                "vod_play_url": "第1集$https://a.example/480-s01e01.m3u8",
            },
        ),
        _result_from_item(
            source_a,
            {
                "vod_id": "a-e2",
                "vod_name": "示例剧 第一季 第2集",
                "vod_year": "2026",
                "type_name": "电视剧",
                "vod_play_url": "第2集$https://a.example/480-s01e02.m3u8",
            },
        ),
        _result_from_item(
            source_b,
            {
                "vod_id": "b-e1",
                "vod_name": "示例剧 第一季 第1集",
                "vod_year": "2026",
                "type_name": "电视剧",
                "vod_play_url": "第1集$https://b.example/1080-s01e01.m3u8",
            },
        ),
    ]
    subscribe = SimpleNamespace(
        state="R",
        name="示例剧",
        year="2026",
        type="电视剧",
        season=1,
        media_source="lunatv",
        media_id="cms-a:a-e1",
        save_path=str(tmp_path),
    )
    subscribe_module = ModuleType("app.db.oper.subscribe")

    class FakeSubscribeOper:
        def list(self, state=None):
            assert state == "R,P"
            return [subscribe]

    subscribe_module.SubscribeOper = FakeSubscribeOper
    app_module = ModuleType("app")
    app_module.__path__ = []
    app_db_module = ModuleType("app.db")
    app_db_module.__path__ = []
    app_db_oper_module = ModuleType("app.db.oper")
    app_db_oper_module.__path__ = []
    app_module.db = app_db_module
    app_db_module.oper = app_db_oper_module
    app_db_oper_module.subscribe = subscribe_module
    monkeypatch.setitem(sys.modules, "app", app_module)
    monkeypatch.setitem(sys.modules, "app.db", app_db_module)
    monkeypatch.setitem(sys.modules, "app.db.oper", app_db_oper_module)
    monkeypatch.setitem(sys.modules, "app.db.oper.subscribe", subscribe_module)

    class Client:
        def search(self, _query, **_kwargs):
            return rows

    probe_calls = []

    def probe(urls):
        probe_calls.append(list(urls))
        return {url: 1080 if "1080" in url else 480 for url in urls}

    plugin = LunaTVSource()
    plugin.init_plugin(
        {
            "enabled": True,
            "download_root": str(tmp_path),
            "source_strategy": "all",
        }
    )
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_prepare_result", lambda result: (result, {}))
    monkeypatch.setattr(plugin, "_probe_resource_urls", probe)

    response = plugin.refresh_subscriptions()
    tasks = plugin._queue.list_tasks()

    assert response["queued"] == 3
    assert all(not urls for urls in probe_calls)
    assert sorted(
        (task["media_id"], task["episode"], task["url"]) for task in tasks
    ) == [
        ("cms-a:a-e1", 1, "https://a.example/480-s01e01.m3u8"),
        ("cms-a:a-e1", 2, "https://a.example/480-s01e02.m3u8"),
        ("cms-b:b-e1", 1, "https://b.example/1080-s01e01.m3u8"),
    ]


def test_native_history_number_parser_supports_ranges_and_native_markers():
    assert LunaTVSource._history_numbers("S02") == {2}
    assert LunaTVSource._history_numbers("E01-E03, E05") == {1, 2, 3, 5}
    assert LunaTVSource._history_numbers("第8至10集、12") == {8, 9, 10, 12}


def test_refresh_does_not_requeue_episode_kept_in_native_tmdb_history(monkeypatch, tmp_path: Path):
    result = _result_from_item(
        CmsSource("cms-demo", "演示源", "https://cms.example/vod"),
        {
            "vod_id": "42",
            "vod_name": "示例剧",
            "vod_year": "2026",
            "type_name": "电视剧",
            "vod_play_url": "S02E03$https://example.test/s02e03.m3u8",
        },
    )
    subscribe = SimpleNamespace(
        state="R",
        name="示例剧",
        year="2026",
        type="电视剧",
        season=2,
        media_source="lunatv",
        media_id="cms-demo:42",
        save_path=str(tmp_path),
    )
    identity_calls = []
    file_calls = []

    class FakeSubscribeOper:
        def list(self, state=None):
            assert state == "R,P"
            return [subscribe]

    class FakeDownloadHistoryOper:
        def get_by_media_identity(self, media_source, media_id):
            identity_calls.append((str(getattr(media_source, "value", media_source)), media_id))
            return [
                SimpleNamespace(
                    download_hash="",
                    seasons="S02",
                    episodes="E01-E03,E05",
                ),
                SimpleNamespace(
                    download_hash="native-history",
                    seasons="S02",
                    episodes="E01-E03,E05",
                ),
            ]

        def get_files_by_hash(self, download_hash, state=None):
            file_calls.append((download_hash, state))
            return [SimpleNamespace(fullpath="/library/示例剧/Season 02/S02E03.mp4", state=1)]

    subscribe_module = ModuleType("app.db.oper.subscribe")
    subscribe_module.SubscribeOper = FakeSubscribeOper
    download_module = ModuleType("app.db.oper.downloadhistory")
    download_module.DownloadHistoryOper = FakeDownloadHistoryOper
    app_module = ModuleType("app")
    app_module.__path__ = []
    app_db_module = ModuleType("app.db")
    app_db_module.__path__ = []
    app_db_oper_module = ModuleType("app.db.oper")
    app_db_oper_module.__path__ = []
    app_module.db = app_db_module
    app_db_module.oper = app_db_oper_module
    app_db_oper_module.subscribe = subscribe_module
    app_db_oper_module.downloadhistory = download_module
    monkeypatch.setitem(sys.modules, "app", app_module)
    monkeypatch.setitem(sys.modules, "app.db", app_db_module)
    monkeypatch.setitem(sys.modules, "app.db.oper", app_db_oper_module)
    monkeypatch.setitem(sys.modules, "app.db.oper.subscribe", subscribe_module)
    monkeypatch.setitem(sys.modules, "app.db.oper.downloadhistory", download_module)

    class Client:
        def search(self, _query, **_kwargs):
            return [result]

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "download_root": str(tmp_path)})
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(
        plugin,
        "_prepare_result",
        lambda item: (item, {
            "status": "matched",
            "media_source": "themoviedb",
            "media_id": "999",
            "title": "TMDB 示例剧",
        }),
    )

    response = plugin.refresh_subscriptions()
    assert response["queued"] == 0
    assert response["reconciled"] == 1
    assert plugin._queue.list_tasks() == []
    assert identity_calls == [("themoviedb", "999")]
    assert file_calls == [("native-history", 1)]


def test_refresh_routes_movie_and_tv_subscriptions_to_native_media_directories(monkeypatch):
    movie = _result_from_item(
        CmsSource("movie-source", "电影源", "https://movie.example/vod"),
        {
            "vod_id": "m1",
            "vod_name": "示例电影",
            "vod_year": "2026",
            "type_name": "电影",
            "vod_play_url": "正片$https://example.test/movie.m3u8",
        },
    )
    show = _result_from_item(
        CmsSource("tv-source", "电视剧源", "https://tv.example/vod"),
        {
            "vod_id": "t1",
            "vod_name": "示例剧",
            "vod_year": "2026",
            "type_name": "电视剧",
            "vod_play_from": "在线播放",
            "vod_play_url": "S01E01$https://example.test/show-s01e01.m3u8",
        },
    )
    subscriptions = [
        SimpleNamespace(state="R", name="示例电影", year="2026", type="电影", season=0,
                        media_source="lunatv", media_id="", save_path=""),
        SimpleNamespace(state="P", name="示例剧", year="2026", type="电视剧", season=1,
                        media_source="lunatv", media_id="", save_path=""),
    ]
    subscribe_module = ModuleType("app.db.oper.subscribe")

    class FakeSubscribeOper:
        def list(self, state=None):
            return subscriptions

    subscribe_module.SubscribeOper = FakeSubscribeOper
    app_module = ModuleType("app")
    app_module.__path__ = []
    app_db_module = ModuleType("app.db")
    app_db_module.__path__ = []
    app_db_oper_module = ModuleType("app.db.oper")
    app_db_oper_module.__path__ = []
    app_module.db = app_db_module
    app_db_module.oper = app_db_oper_module
    app_db_oper_module.subscribe = subscribe_module
    monkeypatch.setitem(sys.modules, "app", app_module)
    monkeypatch.setitem(sys.modules, "app.db", app_db_module)
    monkeypatch.setitem(sys.modules, "app.db.oper", app_db_oper_module)
    monkeypatch.setitem(sys.modules, "app.db.oper.subscribe", subscribe_module)

    class Directory:
        def __init__(self, media_type, path):
            self.storage = "local"
            self.download_path = path
            self.library_path = path.replace("incoming", "library")
            self.media_type = media_type
            self.priority = 1
            self.name = media_type

    class DirectoryHelper:
        def get_download_dirs(self):
            return [Directory("电影", "/media/incoming/movies"), Directory("电视剧", "/media/incoming/tv")]

    class Client:
        def search(self, query, **_kwargs):
            return [movie if query == "示例电影" else show]

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin_module, "_HostDirectoryHelper", DirectoryHelper)
    monkeypatch.setattr(plugin, "_start_queue", lambda: None)
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_prepare_result", lambda item: (item, {}))
    monkeypatch.setattr(plugin._ai, "normalize", lambda title, *_args: (title, False))

    first = plugin.refresh_subscriptions()
    second = plugin.refresh_subscriptions()
    assert first["queued"] == 2
    assert second["queued"] == 0
    tasks = sorted(plugin._queue.list_tasks(), key=lambda item: item["media_type"])
    assert [(task["media_type"], task["root"]) for task in tasks] == [
        ("movie", "/media/incoming/movies"),
        ("tv", "/media/incoming/tv"),
    ]


def test_local_episode_path_requires_completed_download_or_strm_artifact(tmp_path: Path):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})

    for mode in ("download", "strm"):
        task = SimpleNamespace(
            root=str(tmp_path),
            title="示例作品",
            year="2026",
            media_type="movie",
            season=1,
            episode=1,
            url="https://example.test/movie.m3u8",
            mode=mode,
        )
        relative_dir, filename = media_path(
            task.root,
            task.title,
            task.year,
            task.media_type,
            task.season,
            task.episode,
            task.url,
            task.mode,
        )
        output = Path(task.root) / relative_dir / filename
        output.parent.mkdir(parents=True, exist_ok=True)
        assert plugin._local_episode_path(task) is None

        output.write_text("#EXTM3U" if mode == "strm" else "completed", encoding="utf-8")
        assert plugin._local_episode_path(task) == output


def test_record_completion_writes_original_download_output(monkeypatch, tmp_path: Path):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    task = SimpleNamespace(
        task_id="tid-02",
        mode="download",
        source_key="lunatv",
        media_id="lunatv:1",
        media_type="movie",
        title="电影",
        year="2026",
        season=1,
        episode=1,
        root=str(tmp_path),
        completed_at=0,
    )
    captured: list[str] = []
    output = str(tmp_path / "source.mp4")

    monkeypatch.setattr(plugin, "_native_transfer", lambda _task, _output: "moviepilot")
    monkeypatch.setattr(plugin, "_record_native_history", lambda _task, path: captured.append(path))

    plugin._record_completion(task, output)

    assert captured == [output]


def test_record_native_history_skips_missing_idempotency_abi(monkeypatch, tmp_path: Path):
    writes: list[dict] = []
    download_module = ModuleType("app.db.oper.downloadhistory")

    class IncompleteDownloadHistoryOper:
        def add(self, **kwargs):
            writes.append(kwargs)

        def add_files(self, items):
            writes.extend(items)

    download_module.DownloadHistoryOper = IncompleteDownloadHistoryOper
    app_module = ModuleType("app")
    app_module.__path__ = []
    app_db_module = ModuleType("app.db")
    app_db_oper_module = ModuleType("app.db.oper")
    app_db_module.__path__ = []
    app_db_oper_module.__path__ = []
    app_module.db = app_db_module
    app_db_module.oper = app_db_oper_module
    app_db_oper_module.downloadhistory = download_module

    monkeypatch.setitem(sys.modules, "app", app_module)
    monkeypatch.setitem(sys.modules, "app.db", app_db_module)
    monkeypatch.setitem(sys.modules, "app.db.oper", app_db_oper_module)
    monkeypatch.setitem(sys.modules, "app.db.oper.downloadhistory", download_module)

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    task = SimpleNamespace(
        task_id="tid-missing-abi",
        source_key="cms-demo",
        media_id="cms-demo:42",
        media_type="movie",
        title="测试电影",
        year="2026",
        season=1,
        episode=1,
        mode="download",
    )

    plugin._record_native_history(task, str(tmp_path / "movie.mp4"))
    assert writes == []


def test_sync_media_server_runs_async_and_deduplicates_active_sync(monkeypatch):
    sync_calls = []
    started_threads = []

    class MediaServerChain:
        def sync(self, *, server=None):
            sync_calls.append(server)

    class DeferredThread:
        def __init__(self, target, **_kwargs):
            self.target = target
            started_threads.append(self)

        def start(self):
            return None

    monkeypatch.setattr(plugin_module, "_HostMediaServerChain", MediaServerChain)
    monkeypatch.setattr(plugin_module.threading, "Thread", DeferredThread)

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "mediaserver_name": "Emby"})

    assert plugin._sync_media_server() is True
    assert sync_calls == []
    assert len(started_threads) == 1
    assert plugin._media_sync_running is True

    assert plugin._sync_media_server() is False
    assert len(started_threads) == 1

    started_threads[0].target()
    assert sync_calls == ["Emby"]
    assert plugin._media_sync_running is False


def test_record_native_history_ignores_database_errors(monkeypatch, tmp_path: Path):
    download_module = ModuleType("app.db.oper.downloadhistory")

    class BrokenDownloadHistoryOper:
        def get_by_hash(self, _download_hash):
            raise RuntimeError("database unavailable")

        def get_files_by_hash(self, _download_hash, state=None):
            del state
            raise AssertionError("query should stop at the first database failure")

        def add(self, **_kwargs):
            raise AssertionError("must not write after a database failure")

        def add_files(self, _items):
            raise AssertionError("must not write after a database failure")

    download_module.DownloadHistoryOper = BrokenDownloadHistoryOper
    app_module = ModuleType("app")
    app_module.__path__ = []
    app_db_module = ModuleType("app.db")
    app_db_oper_module = ModuleType("app.db.oper")
    app_db_module.__path__ = []
    app_db_oper_module.__path__ = []
    app_module.db = app_db_module
    app_db_module.oper = app_db_oper_module
    app_db_oper_module.downloadhistory = download_module

    monkeypatch.setitem(sys.modules, "app", app_module)
    monkeypatch.setitem(sys.modules, "app.db", app_db_module)
    monkeypatch.setitem(sys.modules, "app.db.oper", app_db_oper_module)
    monkeypatch.setitem(sys.modules, "app.db.oper.downloadhistory", download_module)

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    task = SimpleNamespace(
        task_id="tid-db-error",
        source_key="cms-demo",
        media_id="cms-demo:42",
        media_type="movie",
        title="测试电影",
        year="2026",
        season=1,
        episode=1,
        mode="download",
    )

    plugin._record_native_history(task, str(tmp_path / "movie.mp4"))
