from lunatvsource_test import LunaTVSource
import lunatvsource_test as plugin_module
from lunatvsource_test.cms import CmsSource, _result_from_item


def test_status_exposes_serial_queue_and_ai_fallback():
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "ai_enabled": False})
    status = plugin.api_status()["data"]
    assert status["enabled"] is True
    assert status["queue"]["pending"] == 0
    assert status["ai"]["available"] is False
    assert status["media_source"] == "lunatv"


def test_manual_download_rejects_non_http_url():
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "download_root": "/tmp/lunatv-test"})
    result = plugin.api_download({"url": "file:///tmp/movie.m3u8"})
    assert result["success"] is False
    assert "http/https" in result["message"]


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
    plugin.init_plugin({"enabled": True, "use_moviepilot_dirs": True})
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
    plugin.init_plugin({"enabled": True, "tmdb_association": True})
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
