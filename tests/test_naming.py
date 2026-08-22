from lunatvsource_test.naming import media_path, safe_component


def test_safe_component_removes_path_separators():
    assert safe_component("A/B:C") == "A B C"


def test_movie_path_uses_year():
    directory, filename = media_path("/media/incoming", "示例电影", "2025", "movie", 1, 1, "x.m3u8")
    assert directory == "示例电影 (2025)"
    assert filename == "示例电影 (2025).mp4"


def test_tv_path_contains_season_and_episode():
    directory, filename = media_path("/media/incoming", "示例剧", "2024", "tv", 8, 3, "x.m3u8")
    assert directory == "示例剧 (2024)/Season 08"
    assert filename == "示例剧 (2024) - S08E03.mp4"


def test_strm_path_keeps_url_as_file_content_later():
    directory, filename = media_path("/media/incoming", "示例剧", "2024", "tv", 1, 1, "x.m3u8", mode="strm")
    assert directory.endswith("Season 01")
    assert filename.endswith(".strm")
