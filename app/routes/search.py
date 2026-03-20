from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth import validate_api_key
from app.models.schemas import ArtistResult, SearchRequest, SearchResponse
from app.services.musicbrainz import search_recordings, get_artist_detail

router = APIRouter(tags=["search"])


@router.post("/search", response_model=SearchResponse)
def search(
    body: SearchRequest,
    fast: bool = Query(False, description="Single MB request, best for real-time/typeahead"),
    _key: str = Depends(validate_api_key),
):
    if not body.query.strip() and not (body.title and body.artist):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Provide query or both title and artist")

    results, artists = search_recordings(
        query=body.query.strip(),
        title=body.title,
        artist=body.artist,
        fast=fast,
    )
    return SearchResponse(results=results, artists=artists)


@router.get("/artist/{artist_id}", response_model=ArtistResult)
def artist_detail(
    artist_id: str,
    limit: int = Query(50, ge=1, le=100, description="Max songs to return"),
    _key: str = Depends(validate_api_key),
):
    """Get full artist details with all their songs."""
    artist = get_artist_detail(artist_id, songs_limit=limit)
    if not artist:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artist not found")
    return artist
