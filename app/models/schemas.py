from typing import Optional
from pydantic import BaseModel


# ── Search ────────────────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    query: str = ""
    title: Optional[str] = None
    artist: Optional[str] = None


class SearchResult(BaseModel):
    musicbrainz_id: str
    title: str
    artist: str
    album: Optional[str] = None
    album_art_url: Optional[str] = None
    album_art_fallback: Optional[str] = None  # Release-group fallback if primary 404s
    youtube_query: str
    score: int = 0  # MusicBrainz confidence score 0-100


class ArtistResult(BaseModel):
    musicbrainz_id: str
    name: str
    type: Optional[str] = None       # Group, Person, etc.
    country: Optional[str] = None
    image_url: Optional[str] = None  # Artist photo from Wikimedia Commons
    tags: list[str] = []
    top_songs: list[SearchResult] = []


class SearchResponse(BaseModel):
    results: list[SearchResult]
    artists: list[ArtistResult] = []


# ── Download ──────────────────────────────────────────────────────────────────

class DownloadRequest(BaseModel):
    musicbrainz_id: str


class SongRecord(BaseModel):
    id: str
    musicbrainz_id: Optional[str] = None
    title: str
    artist: str
    album: Optional[str] = None
    album_art_url: Optional[str] = None
    file_path: str  # signed URL to the MP3 in Supabase Storage
    duration_seconds: Optional[int] = None
    genres: list[str] = []
    youtube_video_id: Optional[str] = None
    youtube_title: Optional[str] = None
    downloaded_at: str
    status: str = "downloaded"


# ── Health ────────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    songs_count: int
    storage_used_mb: float
    yt_dlp_version: str


# ── Admin ─────────────────────────────────────────────────────────────────────

class CreateKeyRequest(BaseModel):
    name: str


class ApiKeyEntry(BaseModel):
    key: str
    name: str
    created_at: str
    is_active: bool


class RevokeKeyRequest(BaseModel):
    key: str
