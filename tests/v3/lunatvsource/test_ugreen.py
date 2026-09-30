from types import SimpleNamespace

import pytest

import app.plugins.lunatvsource as plugin_module
from app.plugins.lunatvsource import LunaTVSource


@pytest.mark.parametrize(
    "server_type,result,accepted",
    [("ugreen", True, True), ("ugreen", False, False),
     ("ugreen", None, False), ("emby", None, True)],
)
def test_refresh_respects_ugreen_result(monkeypatch, server_type, result, accepted):
    plugin = LunaTVSource()
    service = SimpleNamespace(
        type=server_type,
        instance=SimpleNamespace(refresh_root_library=lambda: result),
    )
    monkeypatch.setattr(plugin, "_media_server_services", lambda _server: {"NAS": service})

    services, succeeded = plugin._refresh_media_server_library("NAS")

    assert succeeded is accepted
    assert services == ({"NAS": service} if accepted else {})


@pytest.mark.parametrize("media_type", ["movie", "tv"])
def test_ugreen_refresh_visibility_and_native_sync(monkeypatch, media_type):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "mediaserver_name": "绿联云影音"})
    calls = []

    class Ugreen:
        def refresh_root_library(self):
            calls.append("refresh")
            return True

        def get_movies(self, *, title, year):
            calls.append(("movie", title, year))
            return [SimpleNamespace(item_id="1")]

        def get_tv_episodes(self, *, title, year, season):
            calls.append(("tv", title, year, season))
            return "1", {season: [2]}

    class MediaServerHelper:
        def get_services(self, *, name_filters=None):
            assert name_filters == ["绿联云影音"]
            return {"绿联云影音": SimpleNamespace(type="ugreen", instance=Ugreen())}

    class MediaServerChain:
        def sync(self, *, server=None):
            calls.append(("sync", server))

    class ImmediateThread:
        def __init__(self, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(plugin_module, "_HostMediaServerHelper", MediaServerHelper)
    monkeypatch.setattr(plugin_module, "_HostMediaServerChain", MediaServerChain)
    monkeypatch.setattr(plugin_module.threading, "Thread", ImmediateThread)

    assert plugin._sync_media_server(media_probe={
        "media_type": media_type, "title": "示例影片", "year": "2026",
        "season": 1, "episode": 2,
    }) is True

    assert calls == [
        "refresh",
        (("movie", "示例影片", "2026") if media_type == "movie"
         else ("tv", "示例影片", "2026", 1)),
        ("sync", "绿联云影音"),
    ]
    status = plugin.api_status()["data"]["followup_status"]["media_server_sync"]
    assert status["media_server_refreshed"] is True
    assert status["moviepilot_index_synced"] is True
    assert status["media_server_synced"] is True
