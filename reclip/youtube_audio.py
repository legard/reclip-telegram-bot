import re
from urllib.parse import urlparse


RUSSIAN_LANGUAGE = re.compile(r"^ru(?:-|$)", re.IGNORECASE)
RUSSIAN_FILTER = "[language~='^ru(?:-|$)']"


def empty_russian_audio():
    return {"available": False, "formats": []}


def build_russian_extractor_args(provider_url):
    return [
        "youtube:lang=ru;player_client=mweb",
        f"youtubepot-bgutilhttp:base_url={provider_url.rstrip('/')}",
    ]


def _has_audio(fmt):
    return fmt.get("acodec") not in (None, "none")


def _has_video(fmt):
    return fmt.get("vcodec") not in (None, "none")


def _is_russian(language):
    return bool(RUSSIAN_LANGUAGE.match(str(language or "")))


def is_youtube_info(info):
    extractor = info.get("extractor_key") or info.get("extractor") or ""
    return str(extractor).lower() == "youtube"


def is_youtube_url(url):
    host = (urlparse(url).hostname or "").lower()
    return host in {"youtube.com", "youtu.be"} or host.endswith(".youtube.com")


def _original_audio_language(info):
    audio_formats = [fmt for fmt in info.get("formats", []) if _has_audio(fmt) and fmt.get("language")]
    if not audio_formats:
        return None
    marked = [
        fmt for fmt in audio_formats
        if "original" in f'{fmt.get("format_note", "")} {fmt.get("format", "")}'.lower()
    ]
    candidates = marked or audio_formats
    return max(
        candidates,
        key=lambda fmt: fmt.get("language_preference")
        if isinstance(fmt.get("language_preference"), (int, float)) else float("-inf"),
    ).get("language")


def _height_options(heights):
    return [{"height": height, "label": f"{height}p"} for height in sorted(heights, reverse=True)]


def russian_audio_summary(default_info, localized_info):
    if not is_youtube_info(default_info) or _is_russian(_original_audio_language(default_info)):
        return empty_russian_audio()
    localized = localized_info.get("formats", [])
    russian_audio_only = any(_has_audio(fmt) and not _has_video(fmt) and _is_russian(fmt.get("language")) for fmt in localized)
    source = default_info.get("formats", []) if russian_audio_only else localized
    heights = {
        int(fmt["height"])
        for fmt in source
        if fmt.get("height") and _has_video(fmt)
        and (
            (russian_audio_only and not _has_audio(fmt))
            or (_has_audio(fmt) and _is_russian(fmt.get("language")))
        )
    }
    formats = _height_options(heights)
    return {"available": bool(formats), "formats": formats}


def russian_download_available(localized_info, height):
    formats = localized_info.get("formats", [])
    height_matches = lambda fmt: height is None or fmt.get("height") == height
    russian_audio_only = any(_has_audio(fmt) and not _has_video(fmt) and _is_russian(fmt.get("language")) for fmt in formats)
    separate = russian_audio_only and any(_has_video(fmt) and not _has_audio(fmt) and height_matches(fmt) for fmt in formats)
    combined = any(
        _has_video(fmt) and _has_audio(fmt) and _is_russian(fmt.get("language")) and height_matches(fmt)
        for fmt in formats
    )
    return separate or combined


def build_russian_format_selector(height):
    height_filter = "" if height is None else f"[height={height}]"
    h264 = "[vcodec~='^(avc|h264)']"
    return "/".join([
        f"bv{h264}{height_filter}+ba{RUSSIAN_FILTER}",
        f"b{h264}{height_filter}{RUSSIAN_FILTER}",
        f"bv{height_filter}+ba{RUSSIAN_FILTER}",
        f"b{height_filter}{RUSSIAN_FILTER}",
    ])
