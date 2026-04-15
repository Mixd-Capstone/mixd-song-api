import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import validate_api_key
from app.config import STEM_API_URL, STEM_API_KEY
from app.services import storage

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stems"])


@router.get("/stems")
async def list_all_stems(_key: str = Depends(validate_api_key)):
    """List all songs that have stems available, queried directly from Supabase."""
    from app.services.storage import _sb

    # Get all stem records
    result = _sb().table("stems").select("musicbrainz_id, stem_name, file_key").execute()
    if not result.data:
        return {"songs": []}

    # Group stems by musicbrainz_id
    grouped: dict[str, dict[str, str]] = {}
    for row in result.data:
        mbid = row["musicbrainz_id"]
        if mbid not in grouped:
            grouped[mbid] = {}
        # Generate a signed URL for each stem
        signed = _sb().storage.from_("stem-files").create_signed_url(
            f"{row['file_key']}.mp3", 3600
        )
        grouped[mbid][row["stem_name"]] = signed["signedURL"]

    # Build response with song metadata
    songs = []
    for mbid, stems in grouped.items():
        song = storage.get_song_by_mbid(mbid)
        songs.append({
            "musicbrainz_id": mbid,
            "title": song.title if song else mbid,
            "artist": song.artist if song else "",
            "album": song.album if song else "",
            "album_art_url": song.album_art_url if song else None,
            "stems": stems,
        })

    return {"songs": songs}


@router.post("/stems/{musicbrainz_id}")
async def separate_stems(
    musicbrainz_id: str,
    mode: str = "vocals",
    _key: str = Depends(validate_api_key),
):
    """Trigger stem separation for a downloaded song.

    Proxies to the internal stem API, which runs Demucs and uploads
    results to Supabase.

    Modes:
        "vocals" — vocals + instrumental (default)
        "full"   — full 6-stem separation
    """
    if mode not in ("vocals", "full"):
        raise HTTPException(status_code=400, detail="mode must be 'vocals' or 'full'")

    # Look up the song to get its file path
    song = storage.get_song_by_mbid(musicbrainz_id)
    if not song:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Song not found: {musicbrainz_id}",
        )

    # Get the local file path from the song cache
    from app.services.song_cache import get_path
    local_path = get_path(musicbrainz_id)
    if not local_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Song file not found in local cache. Download it first.",
        )

    # Proxy to stem API
    logger.info("Requesting stem separation for %s (mode=%s)", musicbrainz_id, mode)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            resp = await client.post(
                f"{STEM_API_URL}/separate",
                headers={"X-API-Key": STEM_API_KEY},
                data={
                    "file_path": str(local_path),
                    "musicbrainz_id": musicbrainz_id,
                    "mode": mode,
                },
            )
    except httpx.ConnectError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Stem API is unreachable",
        )
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Stem separation timed out",
        )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Stem API error: {resp.text}",
        )

    return resp.json()


@router.get("/stems/{musicbrainz_id}")
async def get_stems(
    musicbrainz_id: str,
    _key: str = Depends(validate_api_key),
):
    """Get available stems for a song with signed playback URLs."""
    from app.services.storage import _sb

    result = (
        _sb().table("stems")
        .select("stem_name, file_key")
        .eq("musicbrainz_id", musicbrainz_id)
        .execute()
    )

    if not result.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No stems found for this song",
        )

    stems = {}
    for row in result.data:
        signed = _sb().storage.from_("stem-files").create_signed_url(
            f"{row['file_key']}.mp3", 3600
        )
        stems[row["stem_name"]] = signed["signedURL"]

    return {"stems": stems}
