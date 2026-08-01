import pytest

from reclip.youtube_audio import (
    build_russian_extractor_args,
    build_russian_format_selector,
    is_youtube_url,
    russian_audio_summary,
    russian_download_available,
)


def audio(format_id, language, preference, note="", *, height=None, video=False):
    return {
        "format_id": format_id,
        "language": language,
        "language_preference": preference,
        "format_note": note,
        "acodec": "aac",
        "vcodec": "h264" if video else "none",
        "height": height,
    }


def video(format_id, height, codec="h264"):
    return {"format_id": format_id, "height": height, "vcodec": codec, "acodec": "none"}


def test_audio_only_russian_track_uses_default_video_heights_and_deduplicates():
    default = {
        "extractor_key": "Youtube",
        "formats": [
            audio("en", "en-US", 10, "original"),
            video("v1080-a", 1080), video("v1080-b", 1080, "vp9"), video("v720", 720),
        ],
    }
    localized = {"formats": [audio("ru.10", "ru", -10, "auto-dubbed")]}
    assert russian_audio_summary(default, localized) == {
        "available": True,
        "formats": [{"height": 1080, "label": "1080p"}, {"height": 720, "label": "720p"}],
    }


def test_combined_russian_track_uses_only_its_own_heights():
    default = {"extractor": "youtube", "formats": [audio("en", "en", 10), video("v1080", 1080)]}
    localized = {"formats": [audio("ru720", "ru-RU", -10, height=720, video=True)]}
    assert russian_audio_summary(default, localized)["formats"] == [{"height": 720, "label": "720p"}]


@pytest.mark.parametrize("extractor", ["vimeo", "generic"])
def test_non_youtube_has_no_russian_option(extractor):
    default = {"extractor": extractor, "formats": [audio("en", "en", 10)]}
    assert russian_audio_summary(default, {"formats": [audio("ru", "ru", -10)]}) == {
        "available": False, "formats": []
    }


def test_original_russian_audio_has_no_separate_option():
    default = {"extractor": "youtube", "formats": [audio("ru", "ru-RU", 10, "original"), video("v", 720)]}
    localized = {"formats": [audio("ru-dub", "ru", -10)]}
    assert russian_audio_summary(default, localized)["available"] is False


def test_fresh_availability_checks_requested_height():
    localized = {"formats": [audio("ru", "ru", -10), video("v720", 720)]}
    assert russian_download_available(localized, 720) is True
    assert russian_download_available(localized, 1080) is False
    assert russian_download_available(localized, None) is True


def test_selector_has_language_filter_in_every_branch_and_never_falls_back():
    selector = build_russian_format_selector(720)
    branches = selector.split("/")
    assert len(branches) == 4
    assert all("language~=" in branch for branch in branches)
    assert all("height=720" in branch for branch in branches)
    assert "bestaudio" not in selector
    assert not selector.endswith("/best")


def test_russian_extractor_args_use_mweb_and_internal_provider():
    assert build_russian_extractor_args("http://bgutil:4416/") == [
        "youtube:lang=ru;player_client=mweb",
        "youtubepot-bgutilhttp:base_url=http://bgutil:4416",
    ]


@pytest.mark.parametrize("url", ["https://youtube.com/watch?v=x", "https://www.youtube.com/shorts/x", "https://youtu.be/x"])
def test_youtube_url_hosts_are_accepted(url):
    assert is_youtube_url(url) is True


def test_lookalike_youtube_host_is_rejected():
    assert is_youtube_url("https://youtube.com.evil.example/watch?v=x") is False
