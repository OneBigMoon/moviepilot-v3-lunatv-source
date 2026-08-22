from lunatvsource_test import LunaTVSource


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

