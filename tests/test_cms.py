import json

from lunatvsource_test.cms import AppleCmsClient, CmsSource, _parse_play_urls, _result_from_item, parse_config


def test_parse_config_filters_by_api_host():
    sources = parse_config(
        {
            "api_site": {
                "suonizy.net": {
                    "name": "索尼",
                    "api": "https://suoniapi.com/api.php/provide/vod",
                    "detail": "https://suonizy.net",
                },
                "other": {"name": "其它", "api": "https://other.example/vod"},
            }
        },
        allowlist=("suonizy.net",),
    )
    assert len(sources) == 1
    assert sources[0].api.endswith("/api.php/provide/vod")


def test_parse_play_urls_reads_multiple_episodes_and_seasons():
    episodes = _parse_play_urls(
        "高清$$$备用",
        "01$https://example.test/s01e01.m3u8#02$https://example.test/s01e02.m3u8$$$01$https://backup.test/01.m3u8",
    )
    assert [(item.season, item.episode) for item in episodes] == [(1, 1), (1, 2)]
    assert episodes[0].url.endswith("s01e01.m3u8")


def test_parse_play_urls_supports_chinese_episode_label():
    episodes = _parse_play_urls("在线播放", "第8集$https://example.test/08.m3u8")
    assert episodes[0].episode == 8


def test_parse_play_urls_preserves_season_groups():
    episodes = _parse_play_urls(
        "第1季$$$第2季",
        "01$https://example.test/s1e01.m3u8$$$01$https://example.test/s2e01.m3u8",
    )
    assert [(item.season, item.episode) for item in episodes] == [(1, 1), (2, 1)]


def test_search_enriches_sparse_list_item_with_detail_play_urls():
    source = CmsSource(
        key="demo",
        name="演示",
        api="https://cms.example/api.php/provide/vod",
    )
    client = AppleCmsClient([source])
    calls = []

    def fake_request(current, **params):
        calls.append(params)
        if params.get("ac") == "list":
            return {"list": [{"vod_id": "42", "vod_name": "示例剧", "type_name": "电视剧"}]}
        return {
            "list": [
                {
                    "vod_id": "42",
                    "vod_play_from": "在线播放",
                    "vod_play_url": "01$https://example.test/01.m3u8#02$https://example.test/02.m3u8",
                }
            ]
        }

    client._request = fake_request
    results = client.search("示例剧")
    assert len(results) == 1
    assert [episode.episode for episode in results[0].episodes] == [1, 2]
    assert {call.get("ac") for call in calls} == {"list", "detail"}


def test_result_uses_title_season_hint_for_multi_season_bundle():
    result = _result_from_item(
        CmsSource(key="demo", name="演示", api="https://cms.example/api.php/provide/vod"),
        {
            "vod_id": "8",
            "vod_name": "海底小纵队中文版 (1-8季)",
            "type_name": "电视剧",
            "vod_play_from": "在线播放",
            "vod_play_url": "01$https://example.test/01.m3u8#02$https://example.test/02.m3u8",
        },
    )
    assert [(item.season, item.episode) for item in result.episodes] == [(1, 1), (1, 2)]

    season_eight = _result_from_item(
        CmsSource(key="demo", name="演示", api="https://cms.example/api.php/provide/vod"),
        {
            "vod_id": "8b",
            "vod_name": "海底小纵队中文版 第8季",
            "type_name": "电视剧",
            "vod_play_from": "在线播放",
            "vod_play_url": "01$https://example.test/08-01.m3u8",
        },
    )
    assert season_eight.episodes[0].season == 8


def test_animation_is_treated_as_series_for_season_naming():
    result = _result_from_item(
        CmsSource(key="demo", name="演示", api="https://cms.example/api.php/provide/vod"),
        {
            "vod_id": "cartoon",
            "vod_name": "示例动画",
            "type_name": "动漫",
            "vod_play_from": "在线播放",
            "vod_play_url": "01$https://example.test/01.m3u8#02$https://example.test/02.m3u8",
        },
    )
    assert result.media_type == "tv"
