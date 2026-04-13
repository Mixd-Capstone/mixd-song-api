import hashlib
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Optional

import yt_dlp

# Patterns that indicate a music video rather than a clean audio track
_VIDEO_PENALTY_PATTERNS = [
    re.compile(r"\bofficial\s*(music\s*)?video\b", re.IGNORECASE),
    re.compile(r"\bmusic\s*video\b", re.IGNORECASE),
    re.compile(r"\bofficial\s*(4K\s*)?video\b", re.IGNORECASE),
    re.compile(r"\(MV\)|\[MV\]", re.IGNORECASE),
    re.compile(r"\bM/?V\b"),
]

# Patterns that indicate a preferred audio-only upload
_AUDIO_BONUS_PATTERNS = [
    re.compile(r"\bofficial\s*audio\b", re.IGNORECASE),
    re.compile(r"\blyric(s)?\s*video\b", re.IGNORECASE),
    re.compile(r"\baudio\b", re.IGNORECASE),
    re.compile(r"\bofficial\s*lyric(s)?\b", re.IGNORECASE),
]

_SEARCH_RESULTS = 5


def _score_result(entry: dict, title: str, artist: str) -> int:
    """Score a YouTube search result. Higher = better for audio download."""
    yt_title = entry.get("title", "")
    score = 0

    for pat in _VIDEO_PENALTY_PATTERNS:
        if pat.search(yt_title):
            score -= 10
            break

    for pat in _AUDIO_BONUS_PATTERNS:
        if pat.search(yt_title):
            score += 10
            break

    # Prefer results from topic/auto-generated channels (e.g. "Artist - Topic")
    channel = entry.get("channel", "") or entry.get("uploader", "") or ""
    if re.search(r"\bTopic\b", channel):
        score += 15

    return score

# Thread-safe set of file keys currently being downloaded
_in_progress: set[str] = set()
_lock = threading.Lock()


def _file_key(musicbrainz_id: Optional[str], title: str, artist: str) -> str:
    if musicbrainz_id:
        return musicbrainz_id
    raw = f"{title.lower().strip()}{artist.lower().strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def is_in_progress(musicbrainz_id: Optional[str], title: str, artist: str) -> bool:
    key = _file_key(musicbrainz_id, title, artist)
    with _lock:
        return key in _in_progress


def download_song(
    musicbrainz_id: Optional[str],
    title: str,
    artist: str,
    timeout_seconds: int = 60,
) -> dict:
    """
    Download a song via yt-dlp into a temp directory and return metadata dict.
    The caller is responsible for uploading and deleting the file.
    Raises RuntimeError on failure, TimeoutError on timeout.
    """
    key = _file_key(musicbrainz_id, title, artist)

    with _lock:
        if key in _in_progress:
            raise RuntimeError("Download already in progress for this song")
        _in_progress.add(key)

    tmp_dir = tempfile.mkdtemp(prefix="mixd_")
    output_path = os.path.join(tmp_dir, f"{key}.%(ext)s")
    final_path = os.path.join(tmp_dir, f"{key}.mp3")

    result: dict = {}
    error: list[Exception] = []

    def _do_download():
        try:
            # Step 1: Search for candidates without downloading
            search_opts = {
                "default_search": f"ytsearch{_SEARCH_RESULTS}",
                "noplaylist": True,
                "quiet": True,
                "skip_download": True,
            }
            with yt_dlp.YoutubeDL(search_opts) as ydl:
                search_info = ydl.extract_info(f"{title} - {artist}", download=False)

            entries = search_info.get("entries", [])
            if not entries:
                raise RuntimeError("No YouTube results found")

            # Step 2: Score and pick the best candidate
            best = max(entries, key=lambda e: _score_result(e, title, artist))

            # Step 3: Download the chosen result
            dl_opts = {
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
                "outtmpl": output_path,
                "noplaylist": True,
                "quiet": True,
            }
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                info = ydl.extract_info(best["webpage_url"], download=True)
                result["info"] = info
        except Exception as exc:
            error.append(exc)

    thread = threading.Thread(target=_do_download, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)

    with _lock:
        _in_progress.discard(key)

    if thread.is_alive():
        raise TimeoutError(f"Download timed out after {timeout_seconds}s")

    if error:
        raise RuntimeError(f"yt-dlp error: {error[0]}") from error[0]

    info = result.get("info", {})

    return {
        "file_path": final_path,
        "duration_seconds": int(info["duration"]) if info.get("duration") else None,
        "youtube_video_id": info.get("id"),
        "youtube_title": info.get("title"),
        "file_key": key,
    }
