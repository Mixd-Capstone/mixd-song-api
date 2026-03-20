import os
from typing import Optional

from supabase import create_client, Client

from app.config import SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_BUCKET
from app.models.schemas import SongRecord

_client: Optional[Client] = None


def _sb() -> Client:
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _client


# ── File upload ───────────────────────────────────────────────────────────────

def upload_file(local_path: str, file_key: str) -> None:
    """Upload MP3 to Supabase Storage then delete the local temp file."""
    storage_path = f"{file_key}.mp3"
    with open(local_path, "rb") as f:
        _sb().storage.from_(SUPABASE_BUCKET).upload(
            path=storage_path,
            file=f,
            file_options={"content-type": "audio/mpeg", "upsert": "true"},
        )
    try:
        os.remove(local_path)
    except OSError:
        pass


def get_signed_url(file_key: str, expires_in: int = 3600) -> str:
    result = _sb().storage.from_(SUPABASE_BUCKET).create_signed_url(
        f"{file_key}.mp3", expires_in
    )
    return result["signedURL"]


# ── Song records ──────────────────────────────────────────────────────────────

def get_song_by_mbid(musicbrainz_id: str) -> Optional[SongRecord]:
    result = (
        _sb().table("songs")
        .select("*")
        .eq("musicbrainz_id", musicbrainz_id)
        .limit(1)
        .execute()
    )
    if result.data:
        return _row_to_record(result.data[0])
    return None


def get_song_by_key(file_key: str) -> Optional[SongRecord]:
    result = (
        _sb().table("songs")
        .select("*")
        .eq("file_key", file_key)
        .limit(1)
        .execute()
    )
    if result.data:
        return _row_to_record(result.data[0])
    return None


def save_song(
    musicbrainz_id: Optional[str],
    title: str,
    artist: str,
    album: Optional[str],
    album_art_url: Optional[str],
    genres: list,
    file_path: str,
    duration_seconds: Optional[int],
    youtube_video_id: Optional[str],
    youtube_title: Optional[str],
    file_key: str,
) -> SongRecord:
    upload_file(file_path, file_key)

    row = {
        "musicbrainz_id": musicbrainz_id,
        "title": title,
        "artist": artist,
        "album": album,
        "album_art_url": album_art_url,
        "file_key": file_key,
        "duration_seconds": duration_seconds,
        "youtube_video_id": youtube_video_id,
        "youtube_title": youtube_title,
        "genres": genres,
    }
    result = _sb().table("songs").insert(row).execute()
    return _row_to_record(result.data[0])


def get_all_downloaded_mbids() -> list[str]:
    """Return all MusicBrainz IDs that have been downloaded."""
    result = _sb().table("songs").select("musicbrainz_id").not_.is_("musicbrainz_id", "null").execute()
    return [r["musicbrainz_id"] for r in result.data if r.get("musicbrainz_id")]


def songs_count() -> int:
    result = _sb().table("songs").select("id", count="exact").execute()
    return result.count or 0


def storage_used_mb() -> float:
    # Supabase Storage doesn't expose total size via REST easily;
    # approximate from song count * average size (or return 0 until Supabase supports it)
    result = _sb().table("songs").select("id", count="exact").execute()
    count = result.count or 0
    return round(count * 4.5, 2)  # rough estimate: ~4.5 MB per song at 192kbps


# ── Internal ──────────────────────────────────────────────────────────────────

def _row_to_record(row: dict) -> SongRecord:
    from app.services import song_cache

    file_key = row["file_key"]

    # Prefer local cache (served via /api/cache/{file_key}) over Supabase signed URL
    if song_cache.has(file_key):
        file_url = f"/api/cache/{file_key}"
    else:
        file_url = get_signed_url(file_key)

    return SongRecord(
        id=row["id"],
        musicbrainz_id=row.get("musicbrainz_id"),
        title=row["title"],
        artist=row["artist"],
        album=row.get("album"),
        album_art_url=row.get("album_art_url"),
        file_path=file_url,
        duration_seconds=row.get("duration_seconds"),
        genres=row.get("genres") or [],
        youtube_video_id=row.get("youtube_video_id"),
        youtube_title=row.get("youtube_title"),
        downloaded_at=row.get("created_at", ""),
        status="downloaded",
    )
