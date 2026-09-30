import app.plugins.lunatvsource as plugin_module
from app.plugins.lunatvsource import LunaTVSource
from app.plugins.lunatvsource.cms import CmsEpisode, CmsResult


def test_movie_native_projection_groups_quality_variants_and_keeps_downloads(monkeypatch):
    class TorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    rows = [
        CmsResult(
            source_key=source,
            source_name=source,
            vod_id=source,
            title="海底小纵队：洞穴大冒险",
            year="2020",
            media_type="movie",
            remark="",
            episodes=(CmsEpisode(1, 1, "正片", f"https://video.example/{height}.m3u8"),),
        )
        for source, height in (("high", 1080), ("low", 720))
    ]

    class Client:
        sources = ()

        def search(self, *_args, **_kwargs):
            return rows

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", TorrentInfo)
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_associate_tmdb", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        plugin, "_probe_resource_urls",
        lambda urls: {url: 1080 if "1080" in url else 720 for url in urls},
    )
    try:
        items = plugin._resource_torrents("海底小纵队：洞穴大冒险", mtype="电影")
        assert len(items) == 2
        assert len({item.title for item in items}) == 1
        assert len({item.description for item in items}) == 1
        payloads = [plugin._decode_resource_token(item.enclosure) for item in items]
        assert [payload["resolution_height"] for payload in payloads] == [1080, 720]
        assert len({payload["url"] for payload in payloads}) == 2
        assert {payload["source_key"] for payload in payloads} == {"high", "low"}
        assert all(payload["resolution"] in item.site_name for item, payload in zip(items, payloads))
        assert all(payload["resolution"] in item.labels for item, payload in zip(items, payloads))
    finally:
        plugin.stop_service()


def test_native_media_cards_merge_sources_without_merging_seasons_or_remakes():
    from dataclasses import replace

    plugin = LunaTVSource()
    base = CmsResult(
        source_key="first", source_name="第一源", vod_id="first",
        title="海底小纵队", year="2010", media_type="tv", remark="",
        episodes=(CmsEpisode(1, 1, "第1集", "https://video.example/first.m3u8"),),
    )
    with_poster = replace(
        base, source_key="second", source_name="第二源", vod_id="second",
        poster_path="https://image.example/poster.jpg",
    )
    season_two = replace(base, episodes=(CmsEpisode(2, 1, "第1集", "https://video.example/s2.m3u8"),))
    remake = replace(base, year="2020")
    movie = replace(base, media_type="movie")
    cards = plugin._prepare_native_media_cards([base, with_poster, season_two, remake, movie])
    assert len(cards) == 4
    season_one = [row for row, _ in cards if row.media_type == "tv" and row.year == "2010" and row.season_range == (1, 1)]
    assert len(season_one) == 1
    assert season_one[0].poster_path == with_poster.poster_path


def test_tv_native_projection_keeps_quality_out_of_card_title_and_description(monkeypatch):
    class TorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    rows = [
        CmsResult(
            source_key="high",
            source_name="高清源",
            vod_id="s02-high",
            title="示例剧",
            year="2024",
            media_type="tv",
            remark="",
            episodes=(CmsEpisode(2, 1, "第1集", "https://video.example/1080-s02.m3u8"),),
        ),
        CmsResult(
            source_key="low",
            source_name="标清源",
            vod_id="s02-low",
            title="示例剧",
            year="2024",
            media_type="tv",
            remark="",
            episodes=(CmsEpisode(2, 1, "第1集", "https://video.example/720-s02.m3u8"),),
        ),
    ]

    class Client:
        sources = ()

        def search(self, *_args, **_kwargs):
            return rows

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", TorrentInfo)
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(plugin, "_associate_tmdb", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        plugin,
        "_probe_resource_urls",
        lambda urls: {url: 1080 if "1080" in url else 720 for url in urls},
    )
    plugin._quality_probe_ms["https://video.example/1080-s02.m3u8"] = 86

    items = plugin._resource_torrents("示例剧", mtype="tv")

    assert [item.pri_order for item in items] == [998108, 998072]
    assert {item.title for item in items} == {"示例剧 (2024) · 第2季"}
    assert all("1080P" not in item.title and "720P" not in item.title for item in items)
    assert all("1080P" not in item.description and "720P" not in item.description for item in items)
    assert items[0].site_name == "高清源 · 1080P · 86ms"
    assert "1080P" in items[0].labels
    assert "86ms" in items[0].labels

    targeted_items = plugin._resource_torrents(
        "示例剧",
        mtype="tv",
        target_media_source="themoviedb",
        target_media_id="selected-tv-123",
        target_media_title="目标剧名",
        target_media_year="2024",
    )
    targeted_payloads = [
        plugin._decode_resource_token(item.enclosure) for item in targeted_items
    ]
    assert {
        (item.media_source, item.media_id) for item in targeted_items
    } == {("themoviedb", "selected-tv-123")}
    assert all(payload["title"] == "目标剧名" for payload in targeted_payloads)
    assert all(payload["year"] == "2024" for payload in targeted_payloads)
    assert all(
        (payload["host_media_source"], payload["host_media_id"])
        == ("themoviedb", "selected-tv-123")
        for payload in targeted_payloads
    )
    assert all(
        (episode["host_media_source"], episode["host_media_id"])
        == ("themoviedb", "selected-tv-123")
        for payload in targeted_payloads
        for episode in payload["episodes"]
    )
