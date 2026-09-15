"""Focused regression tests for the 0.4.107 follow-up audit."""

import base64
import json
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints

import pytest

import app.plugins.lunatvsource as plugin_module
import app.plugins.lunatvsource.cms as cms_module
import app.plugins.lunatvsource.downloader as downloader_module
from app.plugins.lunatvsource import LunaTVSource
from app.plugins.lunatvsource.cms import (
    AppleCmsClient,
    CmsEpisode,
    CmsResult,
    CmsSource,
    _fetch_public_url,
    _result_from_item,
)


class FakeTorrentInfo:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_resource_token_round_trip_preserves_every_episode_field():
    payload = {
        "url": "https://video.example/s01e01.m3u8",
        "title": "示例剧",
        "year": "2017",
        "media_type": "tv",
        "season": 1,
        "episode": 1,
        "episodes": [
            {
                "url": "https://video.example/s01e01.m3u8",
                "title": "示例剧",
                "year": "2017",
                "media_type": "tv",
                "season": 1,
                "episode": 1,
                "label": "第1集",
                "season_known": True,
            },
            {
                "url": "https://video.example/s01e02.m3u8",
                "title": "示例剧",
                "year": "2017",
                "media_type": "tv",
                "season": 1,
                "episode": 2,
                "label": "第2集",
                "season_known": True,
            },
        ],
    }

    token = LunaTVSource._resource_token(payload)

    assert LunaTVSource._decode_resource_token(token) == payload


def test_api_download_rejects_malformed_resource_token(monkeypatch, tmp_path: Path):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "download_root": str(tmp_path)})
    monkeypatch.setattr(plugin, "_start_queue", lambda: None)
    bad_json = base64.urlsafe_b64encode(b"not-json").decode("ascii").rstrip("=")

    response = plugin.api_download(
        {"content": f"magnet:?xt=urn:btih:invalid&x.lunatv={bad_json}"}
    )

    assert response["success"] is False
    assert plugin._queue.list_tasks() == []


def test_api_download_token_queues_all_episode_entries(monkeypatch, tmp_path: Path):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "download_root": str(tmp_path)})
    monkeypatch.setattr(plugin, "_start_queue", lambda: None)
    token = plugin._resource_token(
        {
            "title": "示例剧",
            "year": "2017",
            "media_type": "tv",
            "media_id": "demo:show",
            "source_key": "demo",
            "source_name": "演示源",
            "episodes": [
                {
                    "url": "https://video.example/s01e01.m3u8",
                    "title": "示例剧",
                    "year": "2017",
                    "media_type": "tv",
                    "season": 1,
                    "episode": 1,
                    "label": "第1集",
                    "season_known": True,
                },
                {
                    "url": "https://video.example/s01e02.m3u8",
                    "title": "示例剧",
                    "year": "2017",
                    "media_type": "tv",
                    "season": 1,
                    "episode": 2,
                    "label": "第2集",
                    "season_known": True,
                },
            ],
        }
    )

    response = plugin.api_download({"content": token})

    assert response["success"] is True
    tasks = plugin._queue.list_tasks()
    assert sorted(
        (
            task["season"],
            task["episode"],
            task["year"],
            task["url"],
        )
        for task in tasks
    ) == [
        (1, 1, "2017", "https://video.example/s01e01.m3u8"),
        (1, 2, "2017", "https://video.example/s01e02.m3u8"),
    ]


def test_resource_torrents_same_source_tv_season_years_share_tmdb_year(monkeypatch):
    source = CmsSource("demo", "演示源", "https://cms.example/vod")
    results = [
        _result_from_item(
            source,
            {
                "vod_id": vod_id,
                "vod_name": "示例剧 第一季",
                "vod_year": year,
                "type_name": "电视剧",
                # Same URL proves these are duplicate CMS rows whose year
                # metadata differs, not two genuinely different releases.
                "vod_play_url": "第1集$https://video.example/shared.m3u8",
            },
        )
        for vod_id, year in (("2018-row", "2018"), ("2019-row", "2019"))
    ]

    class Client:
        def search(self, *_args, **_kwargs):
            return list(results)

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})
    monkeypatch.setattr(plugin_module, "_HostTorrentInfo", FakeTorrentInfo)
    monkeypatch.setattr(plugin, "_client", lambda: Client())
    monkeypatch.setattr(
        plugin,
        "_associate_tmdb",
        lambda *_args, **_kwargs: {
            "status": "matched",
            "title": "示例剧",
            "year": "2017",
            "media_source": "themoviedb",
            "media_id": "7",
        },
    )
    monkeypatch.setattr(plugin, "_probe_resource_urls", lambda _urls: {})

    resources = plugin.search_torrents({}, "示例剧", mtype="电视剧")

    assert len(resources) == 1
    payload = plugin._decode_resource_token(resources[0].enclosure)
    assert payload["title"] == "示例剧"
    assert payload["year"] == "2017"
    assert {episode["year"] for episode in payload["episodes"]} == {"2017"}


def test_api_download_episode_payload_uses_tmdb_series_year(monkeypatch, tmp_path: Path):
    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True, "download_root": str(tmp_path)})
    calls = []

    def associate(result, include_candidates=True):
        calls.append((result.title, result.year, result.media_type, include_candidates))
        return {"status": "matched", "year": "2017"}

    monkeypatch.setattr(plugin, "_associate_tmdb", associate)
    monkeypatch.setattr(plugin, "_start_queue", lambda: None)

    response = plugin.api_download(
        {
            "title": "数字积木 第3季",
            "year": "2018",
            "media_type": "tv",
            "media_id": "demo:7",
            "episodes": [
                {
                    "url": "https://video.example/s03e01.m3u8",
                    "title": "数字积木 第3季",
                    "year": "2018",
                    "media_type": "tv",
                    "season": 3,
                    "episode": 1,
                    "season_known": True,
                }
            ],
        }
    )

    assert response["success"] is True
    assert calls == [("数字积木 第3季", "2018", "tv", False)]
    assert plugin._queue.list_tasks()[0]["year"] == "2017"


def test_resource_token_rejects_payload_tampering():
    original = {
        "url": "https://video.example/original.m3u8",
        "title": "示例电影",
        "year": "2026",
        "media_type": "movie",
    }
    token = LunaTVSource._resource_token(original)
    parts = urllib.parse.urlsplit(token)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    encoded = query["x.lunatv"]
    encoded += "=" * (-len(encoded) % 4)
    tampered = json.loads(
        base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")
    )
    tampered["url"] = "https://video.example/changed.m3u8"
    query["x.lunatv"] = base64.urlsafe_b64encode(
        json.dumps(tampered, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).decode("ascii").rstrip("=")
    tampered_token = urllib.parse.urlunsplit(
        parts._replace(query=urllib.parse.urlencode(query))
    )

    assert LunaTVSource._decode_resource_token(tampered_token) is None


def test_cms_search_enforces_total_episode_row_budget(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(
        cms_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )
    client = AppleCmsClient(
        [CmsSource("slow", "慢源", "https://slow.example/vod")],
        timeout=1.0,
        parallel_wait_timeout=0.05,
    )

    calls = []

    def slow_request(*_args, **kwargs):
        calls.append(kwargs)
        clock[0] = 0.05
        return {"list": []}

    client._request = slow_request
    client.search(
        "示例剧",
        limit=1,
        max_workers=1,
        expand_tv_episode_rows=True,
        parallel_wait_timeout=0.05,
    )

    assert len(calls) == 1
    assert clock[0] == pytest.approx(0.05)


def test_public_fetch_enforces_body_deadline(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(
        cms_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )

    class Response:
        @staticmethod
        def read(_limit):
            clock[0] += 0.20
            return b"slow-body"

    class Connection:
        closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        cms_module,
        "_request_public_url",
        lambda *_args, **_kwargs: (Connection(), Response(), "https://video.example/a"),
    )

    with pytest.raises(TimeoutError, match="deadline"):
        _fetch_public_url(
            "https://video.example/a",
            timeout=1.0,
            limit=1024,
            deadline=0.05,
        )


def test_tmdb_error_cache_allows_retry_after_transient_failure(monkeypatch):
    calls = []

    class MetaInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class MediaChain:
        def recognize_media(self, **_kwargs):
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError("temporary failure")
            return SimpleNamespace(
                media_id="7",
                tmdb_id=7,
                media_source=SimpleNamespace(value="themoviedb"),
                title="示例剧",
                year="2017",
                seasons={},
            )

    plugin = LunaTVSource()
    plugin.save_data = lambda *_args, **_kwargs: None
    monkeypatch.setattr(plugin_module, "_HostMediaChain", MediaChain)
    monkeypatch.setattr(plugin_module, "_HostMetaInfo", MetaInfo)
    monkeypatch.setattr(
        plugin_module,
        "_HostMediaSource",
        SimpleNamespace(TMDB="themoviedb"),
    )
    result = CmsResult(
        source_key="demo",
        source_name="演示源",
        vod_id="7",
        title="示例剧",
        year="2018",
        media_type="tv",
        remark="",
    )

    first = plugin._associate_tmdb(result, include_candidates=False)
    second = plugin._associate_tmdb(result, include_candidates=False)

    assert first["status"] == "error"
    assert second["status"] == "matched"
    assert len(calls) == 2


def test_nfo_collection_injection_does_not_misread_setid(tmp_path: Path):
    nfo_path = tmp_path / "movie.nfo"
    nfo_path.write_text(
        "<movie><setid>123</setid><title>示例电影</title></movie>",
        encoding="utf-8",
    )

    assert plugin_module.inject_nfo_movie_set(nfo_path, "示例合集", 123)
    assert "<set>" in nfo_path.read_text(encoding="utf-8")


def test_video_file_path_accepts_m2ts(tmp_path: Path):
    path = tmp_path / "episode.m2ts"
    path.write_bytes(b"video")

    assert plugin_module._video_file_path(str(path)) == path


def test_cms_search_type_hints_resolve():
    hints = get_type_hints(AppleCmsClient.search)

    assert "progress_callback" in hints


def test_mpegts_payload_offset_handles_short_jpeg_prefix():
    data = bytearray(b"\xff\xd8\xff" + b"\x00" * 376)
    data[3] = 0x47
    data[191] = 0x47

    assert downloader_module._mpegts_payload_offset(bytes(data)) == 0
