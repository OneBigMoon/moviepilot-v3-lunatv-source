from pathlib import Path

import lunatvsource_test as plugin_module
from lunatvsource_test import LunaTVSource
from lunatvsource_test.cms import CmsSource, _result_from_item


class FakeTorrentInfo:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _configured_plugin(monkeypatch, results):
    class Client:
        def search(self, *_args, **_kwargs):
            return list(results)

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", FakeTorrentInfo)
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_associate_tmdb", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        plugin_module,
        "probe_stream_height",
        lambda url, *_args, **_kwargs: 1080 if "1080" in url else 480,
    )
    return plugin


def test_search_movie_resources_are_sorted_and_download_queues_highest_resolution(
    monkeypatch, tmp_path: Path
):
    source = CmsSource("demo", "演示源", "https://cms.example/vod")
    low = _result_from_item(
        source,
        {
            "vod_id": "movie-low",
            "vod_name": "示例电影",
            "type_name": "电影",
            "vod_play_url": "正片$https://video.example/480.m3u8",
        },
    )
    high = _result_from_item(
        source,
        {
            "vod_id": "movie-high",
            "vod_name": "示例电影",
            "type_name": "电影",
            "vod_play_url": "正片$https://video.example/1080.m3u8",
        },
    )
    plugin = _configured_plugin(monkeypatch, [low, high])

    resources = plugin.search_torrents(
        {"id": "demo"}, "示例电影", mtype="电影"
    )

    assert [item.title for item in resources] == [
        "示例电影 · 1080P",
        "示例电影 · 480P",
    ]
    assert [item.pri_order for item in resources] == [1080, 480]
    assert [
        plugin._decode_resource_token(item.enclosure)["resolution"]
        for item in resources
    ] == ["1080P", "480P"]

    result = plugin.download(resources[0].enclosure, tmp_path)

    assert result[0] == "LunaTVSource"
    tasks = plugin._queue._read()
    assert len(tasks) == 1
    assert tasks[0].url == "https://video.example/1080.m3u8"


def test_search_tv_resources_are_season_cards_and_download_runs_episodes_serially(
    monkeypatch, tmp_path: Path
):
    source = CmsSource("demo", "演示源", "https://cms.example/vod")
    low = _result_from_item(
        source,
        {
            "vod_id": "season-low",
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
            "vod_id": "season-high",
            "vod_name": "示例剧 第一季",
            "type_name": "电视剧",
            "vod_play_url": (
                "第1集$https://video.example/1080-e1.m3u8#"
                "第2集$https://video.example/1080-e2.m3u8"
            ),
        },
    )
    plugin = _configured_plugin(monkeypatch, [low, high])

    resources = plugin.search_torrents(
        {"id": "demo"}, "示例剧", mtype="电视剧"
    )

    assert [item.title for item in resources] == [
        "示例剧 · 第1季 · 1080P",
        "示例剧 · 第1季 · 480P",
    ]
    assert all("集" not in item.title for item in resources)
    assert [item.pri_order for item in resources] == [1080, 480]
    high_payload = plugin._decode_resource_token(resources[0].enclosure)
    assert [episode["episode"] for episode in high_payload["episodes"]] == [1, 2]

    result = plugin.download(resources[0].enclosure, tmp_path)

    assert result[0] == "LunaTVSource"
    queued = plugin._queue._read()
    assert [(task.season, task.episode, task.url) for task in queued] == [
        (1, 1, "https://video.example/1080-e1.m3u8"),
        (1, 2, "https://video.example/1080-e2.m3u8"),
    ]

    executed = []

    def fake_execute(task):
        executed.append((task.episode, task.url))
        return str(tmp_path / f"episode-{task.episode}.mp4")

    plugin._queue._execute = fake_execute
    assert plugin._queue.run_one()["state"] == "completed"
    assert plugin._queue.run_one()["state"] == "completed"
    assert executed == [
        (1, "https://video.example/1080-e1.m3u8"),
        (2, "https://video.example/1080-e2.m3u8"),
    ]
