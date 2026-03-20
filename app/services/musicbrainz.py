from typing import Optional
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import time
import hashlib
import threading
import httpx
import musicbrainzngs

from app.models.schemas import SearchResult, ArtistResult
from app.config import SEARCH_CACHE_MAX_SIZE

musicbrainzngs.set_useragent("MixdSongAPI", "1.0", "https://github.com/mixd/song-api")

COVER_ART_BASE = "https://coverartarchive.org/release"
MIN_SCORE = 80

# Patterns that indicate a cover, remix, or derivative work
_DERIVATIVE_RE = re.compile(
    r'\b(cover|remix|remixed|bootleg|karaoke|instrumental|8[- ]?bit|tribute|acoustic version|made famous)\b',
    re.IGNORECASE,
)

# --- LRU cache with TTL and max size ---
_cache_lock = threading.Lock()
_search_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_CACHE_TTL = 3600  # 1 hour
_CACHE_MAX = SEARCH_CACHE_MAX_SIZE


def _cache_key(*args) -> str:
    return hashlib.md5(str(args).encode()).hexdigest()


def _cache_get(key: str) -> Optional[dict]:
    with _cache_lock:
        if key in _search_cache:
            ts, data = _search_cache[key]
            if time.monotonic() - ts < _CACHE_TTL:
                _search_cache.move_to_end(key)
                return data
            else:
                del _search_cache[key]
    return None


def _cache_put(key: str, data: dict):
    with _cache_lock:
        _search_cache[key] = (time.monotonic(), data)
        _search_cache.move_to_end(key)
        while len(_search_cache) > _CACHE_MAX:
            _search_cache.popitem(last=False)


def _cached_mb_search(**kwargs) -> dict:
    key = _cache_key("search", kwargs)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = musicbrainzngs.search_recordings(**kwargs)
    _cache_put(key, result)
    return result


def _cached_mb_artist_search(**kwargs) -> dict:
    key = _cache_key("artist_search", kwargs)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = musicbrainzngs.search_artists(**kwargs)
    _cache_put(key, result)
    return result


def _get_artist_image_url(artist_id: str) -> Optional[str]:
    """Get artist image URL via MusicBrainz url-rels -> Wikimedia Commons."""
    cache_key = _cache_key("artist_image", artist_id)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached.get("url")

    image_url = None
    try:
        result = musicbrainzngs.get_artist_by_id(artist_id, includes=["url-rels"])
        rels = result.get("artist", {}).get("url-relation-list", [])

        wiki_file = None
        for rel in rels:
            if rel.get("type") == "image":
                target = rel.get("target", "")
                if "commons.wikimedia.org" in target:
                    # Extract filename from URL like .../File:Greenday2010.jpg
                    parts = target.split("File:")
                    if len(parts) == 2:
                        wiki_file = "File:" + parts[1]
                    break

        if wiki_file:
            # Query Wikimedia Commons API for the actual thumbnail URL
            resp = httpx.get(
                "https://commons.wikimedia.org/w/api.php",
                params={
                    "action": "query",
                    "titles": wiki_file,
                    "prop": "imageinfo",
                    "iiprop": "url",
                    "iiurlwidth": 300,
                    "format": "json",
                },
                headers={"User-Agent": "MixdSongAPI/1.0"},
                timeout=5,
            )
            if resp.status_code == 200:
                pages = resp.json().get("query", {}).get("pages", {})
                for page in pages.values():
                    ii = page.get("imageinfo", [{}])[0]
                    image_url = ii.get("thumburl")
                    break
    except Exception:
        pass

    _cache_put(cache_key, {"url": image_url})
    return image_url


def _cover_art_urls(release_id: Optional[str], release_group_id: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """Return (primary_url, fallback_url) for Cover Art Archive.
    Primary = release-specific, Fallback = release-group (higher hit rate).
    Client tries primary first, falls back on 404."""
    primary = f"{COVER_ART_BASE}/{release_id}/front-250" if release_id else None
    fallback = f"https://coverartarchive.org/release-group/{release_group_id}/front-250" if release_group_id else None
    # If no release, promote fallback to primary
    if not primary and fallback:
        return fallback, None
    return primary, fallback


def _get_cover_art_url(release_id: str, release_group_id: Optional[str] = None,
                       title: Optional[str] = None, artist: Optional[str] = None) -> Optional[str]:
    """Verified cover art lookup — used during download, not search.
    Tries CAA release -> CAA release-group -> iTunes as fallbacks."""
    for endpoint in filter(None, [
        f"{COVER_ART_BASE}/{release_id}",
        f"{COVER_ART_BASE}-group/{release_group_id}" if release_group_id else None,
    ]):
        try:
            response = httpx.get(endpoint, timeout=3, follow_redirects=True)
            if response.status_code == 200:
                for image in response.json().get("images", []):
                    if image.get("front"):
                        return image.get("image") or image.get("thumbnails", {}).get("large")
        except Exception:
            pass

    # iTunes fallback
    if title and artist:
        try:
            resp = httpx.get(
                "https://itunes.apple.com/search",
                params={"term": f"{artist} {title}", "media": "music", "entity": "album", "limit": "1"},
                timeout=5,
            )
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                if results:
                    art = results[0].get("artworkUrl100", "")
                    if art:
                        return art.replace("100x100bb", "600x600bb")
        except Exception:
            pass

    return None


def _clean_album(name: Optional[str]) -> Optional[str]:
    if name:
        # Strip ASCII and Unicode smart quotes (\u201c " and \u201d ")
        name = name.strip().strip('"\u201c\u201d')
    return name or None


def _release_rank(release: dict) -> int:
    """
    Lower is better. Rank releases so official studio albums sort first,
    then singles, then compilations/live recordings.
    """
    rg = release.get("release-group", {})
    secondary = rg.get("secondary-type-list", [])
    primary = rg.get("primary-type", "")

    if secondary:  # Compilation, Live, DJ-mix, etc.
        return 2
    if primary == "Album":
        return 0
    if primary in ("Single", "EP"):
        return 1
    return 3


def _best_release(release_list: list) -> Optional[dict]:
    if not release_list:
        return None
    return min(release_list, key=_release_rank)


def _recording_has_official_album(rec: dict) -> bool:
    for r in rec.get("release-list", []):
        if _release_rank(r) == 0:
            return True
    return False


def _popularity_sort_key(rec: dict, result: "SearchResult") -> tuple:
    """
    Sort key for search results.  Lower = better.
    Priority:  (-score_bucket, is_derivative, no_official_album, -release_count)

    Score is bucketed into tiers so a 100-score original always beats
    a 50-score mashup, but among same-score results we prefer originals
    with more releases.
    """
    title = result.title
    is_derivative = 1 if _DERIVATIVE_RE.search(title) else 0
    has_official = any(_release_rank(r) == 0 for r in rec.get("release-list", []))
    release_count = len(rec.get("release-list", []))
    # Bucket: 100-90 = tier 0, 89-70 = tier 1, 69-50 = tier 2, <50 = tier 3
    score_tier = 0 if result.score >= 90 else (1 if result.score >= 70 else (2 if result.score >= 50 else 3))
    return (score_tier, is_derivative, 0 if has_official else 1, -release_count)


def _parse_recordings(recordings: list, min_score: int = MIN_SCORE) -> list[SearchResult]:
    paired: list[tuple[dict, SearchResult]] = []
    for rec in recordings:
        score = int(rec.get("ext:score", 0))
        if score < min_score:
            continue

        title = rec.get("title", "")
        mbid = rec.get("id", "")

        artist = ""
        for credit in rec.get("artist-credit", []):
            if isinstance(credit, dict) and "artist" in credit:
                artist = credit["artist"].get("name", "")
                break

        if not title or not artist:
            continue

        best = _best_release(rec.get("release-list", []))
        album = _clean_album(best.get("title")) if best else None
        release_id = best.get("id") if best else None
        rg_id = best.get("release-group", {}).get("id") if best else None

        art_url, art_fallback = _cover_art_urls(release_id, rg_id)

        sr = SearchResult(
            musicbrainz_id=mbid, title=title, artist=artist,
            album=album, album_art_url=art_url, album_art_fallback=art_fallback,
            youtube_query=f"{title} - {artist}", score=score,
        )
        paired.append((rec, sr))

    # Sort: originals first, then by release count (popularity proxy), then score
    paired.sort(key=lambda p: _popularity_sort_key(p[0], p[1]))
    return [sr for _, sr in paired]


def _field_search(title: str, artist: str, limit: int) -> list[SearchResult]:
    """Use Lucene field syntax: recording:'X' AND artist:'Y' — most precise."""
    result = _cached_mb_search(
        query=f'recording:"{title}" AND artist:"{artist}"',
        limit=limit,
    )
    return _parse_recordings(result.get("recording-list", []))


def _field_search_raw(title: str, artist: str, limit: int) -> list:
    """Run a MusicBrainz field search and return raw recording list (no art fetch)."""
    result = _cached_mb_search(
        query=f'recording:"{title}" AND artist:"{artist}"',
        limit=limit,
    )
    return result.get("recording-list", [])


def _all_splits(words: list[str]) -> list[tuple[str, str]]:
    """Generate all possible (title, artist) splits in BOTH directions."""
    splits = []
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        splits.append((a, b))  # title-first: "green day" / "welcome to paradise"
        splits.append((b, a))  # artist-first: "welcome to paradise" / "green day"
    return splits


def _best_split_search(query: str, limit: int) -> list[SearchResult]:
    """
    For free-text like 'green day welcome to paradise', try every possible
    title/artist split in BOTH directions in parallel.
    """
    words = query.split()
    if len(words) < 2:
        result = _cached_mb_search(
            query=f'recording:"{query}"',
            limit=limit * 2,
        )
        return _parse_recordings(result.get("recording-list", []))

    splits = _all_splits(words)

    # Fire all split searches in parallel (MB calls are I/O-bound)
    best_recordings: list = []
    best_top_score = -1

    with ThreadPoolExecutor(max_workers=len(splits)) as pool:
        futures = {
            pool.submit(_field_search_raw, title, artist, limit * 2): (title, artist)
            for title, artist in splits
        }
        for fut in as_completed(futures):
            recs = fut.result()
            if recs:
                top_score = int(recs[0].get("ext:score", 0))
                if top_score > best_top_score:
                    best_top_score = top_score
                    best_recordings = recs

    return _parse_recordings(best_recordings)[:limit]


def _extract_genres(genre_list: list) -> list[str]:
    """Return genre names sorted by vote count descending."""
    genres = sorted(genre_list, key=lambda g: int(g.get("count", 0)), reverse=True)
    return [g["name"] for g in genres if g.get("name")]


def lookup_recording(mbid: str) -> dict:
    """Fetch title, artist, album, album_art_url, genres for a known MusicBrainz recording ID."""
    result = musicbrainzngs.get_recording_by_id(mbid, includes=["artists", "releases"])
    rec = result.get("recording", {})

    title = rec.get("title", "")

    artist = ""
    for credit in rec.get("artist-credit", []):
        if isinstance(credit, dict) and "artist" in credit:
            artist = credit["artist"].get("name", "")
            break

    album: Optional[str] = None
    album_art_url: Optional[str] = None
    rg_id: Optional[str] = None

    # lookup_recording doesn't return release-group type, so search to get
    # full release-group metadata and pick the best release across all matches
    try:
        search = musicbrainzngs.search_recordings(
            query=f'recording:"{title}" AND artist:"{artist}"',
            limit=20,
        )
        candidates = search.get("recording-list", [])

        # First try to find a good release on the exact MBID
        for candidate in candidates:
            if candidate.get("id") == mbid:
                best = _best_release(candidate.get("release-list", []))
                if best and _release_rank(best) <= 1:
                    album = _clean_album(best.get("title"))
                    release_id = best.get("id")
                    rg_id = best.get("release-group", {}).get("id")
                    if release_id:
                        album_art_url = _get_cover_art_url(release_id, rg_id, title, artist)
                break

        # If still only compilations, find the best official album release
        # across all recordings with the same title/artist
        if not album or (album and _release_rank(
            _best_release([r for c in candidates for r in c.get("release-list", []) if c.get("id") == mbid]) or {}
        ) == 2):
            for candidate in sorted(candidates, key=lambda c: (
                0 if any(_release_rank(r) == 0 for r in c.get("release-list", [])) else 1
            )):
                best = _best_release(candidate.get("release-list", []))
                if best and _release_rank(best) == 0:
                    album = _clean_album(best.get("title"))
                    release_id = best.get("id")
                    rg_id = best.get("release-group", {}).get("id")
                    if release_id:
                        album_art_url = _get_cover_art_url(release_id, rg_id, title, artist)
                    break
    except Exception:
        pass

    # Last resort: use first release from direct lookup
    if not album:
        release_list = rec.get("release-list", [])
        if release_list:
            album = release_list[0].get("title")

    # Fetch release group tags (used as genres)
    genres: list[str] = []
    if rg_id:
        try:
            rg_result = musicbrainzngs.get_release_group_by_id(rg_id, includes=["tags"])
            genres = _extract_genres(rg_result.get("release-group", {}).get("tag-list", []))
        except Exception:
            pass

    return {
        "title": title,
        "artist": artist,
        "album": album,
        "album_art_url": album_art_url,
        "genres": genres,
    }


def _get_artist_top_songs(artist_id: str, top_n: int = 5) -> list[SearchResult]:
    """Fetch top songs for an artist, ranked by release count (popularity proxy)."""
    try:
        # Fetch multiple pages for better coverage
        all_recs = []
        for offset in [0, 100]:
            recs_result = _cached_mb_search(
                query=f'arid:{artist_id}',
                limit=100,
                offset=offset,
            )
            all_recs.extend(recs_result.get("recording-list", []))
            if len(recs_result.get("recording-list", [])) < 100:
                break

        # Deduplicate by lowercase title, keep highest release count
        seen: dict[str, tuple[dict, int]] = {}
        for rec in all_recs:
            title = rec.get("title", "").strip()
            title_lower = title.lower()
            if _DERIVATIVE_RE.search(title):
                continue
            if re.search(r'\((demo|live|acoustic|remix|edit|version)\)', title, re.IGNORECASE):
                continue
            count = len(rec.get("release-list", []))
            if title_lower not in seen or count > seen[title_lower][1]:
                seen[title_lower] = (rec, count)

        top = sorted(seen.values(), key=lambda x: -x[1])[:top_n]
        return _parse_recordings([r for r, _ in top], min_score=0)
    except Exception:
        return []


def _search_artist(query: str, top_n: int = 5) -> list[ArtistResult]:
    """Search for artists matching the query. For strong matches, fetch their top songs."""
    try:
        result = _cached_mb_artist_search(query=query, limit=3)
    except Exception:
        return []

    artists: list[ArtistResult] = []
    for a in result.get("artist-list", []):
        score = int(a.get("ext:score", 0))
        if score < 90:
            continue

        artist_id = a.get("id", "")
        name = a.get("name", "")
        if not artist_id or not name:
            continue

        # Fetch image and top songs in parallel
        with ThreadPoolExecutor(max_workers=2) as pool:
            img_future = pool.submit(_get_artist_image_url, artist_id)
            songs_future = pool.submit(_get_artist_top_songs, artist_id, top_n)

        image_url = img_future.result()
        top_songs = songs_future.result()
        tags = [t.get("name", "") for t in a.get("tag-list", [])[:8] if t.get("name")]

        artists.append(ArtistResult(
            musicbrainz_id=artist_id,
            name=name,
            type=a.get("type"),
            country=a.get("country"),
            image_url=image_url,
            tags=tags,
            top_songs=top_songs,
        ))

    return artists


def get_artist_detail(artist_id: str, songs_limit: int = 50) -> Optional[ArtistResult]:
    """Get full artist details with all songs for the artist page."""
    try:
        result = musicbrainzngs.get_artist_by_id(artist_id, includes=["tags"])
    except Exception:
        return None

    a = result.get("artist", {})
    name = a.get("name", "")
    if not name:
        return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        img_future = pool.submit(_get_artist_image_url, artist_id)
        songs_future = pool.submit(_get_artist_top_songs, artist_id, songs_limit)

    image_url = img_future.result()
    top_songs = songs_future.result()
    tags = [t.get("name", "") for t in a.get("tag-list", [])[:8] if t.get("name")]

    return ArtistResult(
        musicbrainz_id=artist_id,
        name=name,
        type=a.get("type"),
        country=a.get("country"),
        image_url=image_url,
        tags=tags,
        top_songs=top_songs,
    )


def _search_recordings_only(
    query: str,
    title: Optional[str] = None,
    artist: Optional[str] = None,
    limit: int = 15,
    fast: bool = False,
) -> list[SearchResult]:
    """Core recording search logic."""
    # 1. Explicit title + artist — most precise, always a single request
    if title and artist:
        results = _field_search(title, artist, limit)
        if results:
            return results

    # fast=True: try likely splits in both directions in parallel, fall back to free text
    if fast:
        words = query.split()
        if len(words) >= 2:
            splits = []
            for i in range(1, min(len(words), 4)):
                a, b = " ".join(words[:i]), " ".join(words[i:])
                splits.append((a, b))
                splits.append((b, a))

            best_recs: list = []
            best_score = -1
            with ThreadPoolExecutor(max_workers=len(splits)) as pool:
                futures = {
                    pool.submit(_field_search_raw, t, a, limit * 4): (t, a)
                    for t, a in splits
                }
                for fut in as_completed(futures):
                    recs = fut.result()
                    if recs:
                        top = int(recs[0].get("ext:score", 0))
                        if top > best_score:
                            best_score = top
                            best_recs = recs

            if best_recs and best_score >= 80:
                return _parse_recordings(best_recs, min_score=50)[:limit]

        result = _cached_mb_search(query=query, limit=limit * 4)
        return _parse_recordings(result.get("recording-list", []), min_score=50)[:limit]

    # 2. "Title - Artist" or "Artist - Title" dash convention — try both
    if " - " in query:
        parts = query.split(" - ", 1)
        a, b = parts[0].strip(), parts[1].strip()
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(_field_search, a, b, limit)
            f2 = pool.submit(_field_search, b, a, limit)
        r1, r2 = f1.result(), f2.result()
        best = r1 if (r1 and (not r2 or r1[0].score >= r2[0].score)) else r2
        if best:
            return best

    # 3. Free text — try all title/artist splits and pick the best
    results = _best_split_search(query, limit)
    if results:
        return results

    # 4. Last resort: plain query with lower score threshold
    fallback = _cached_mb_search(query=query, limit=limit * 2)
    return _parse_recordings(fallback.get("recording-list", []), min_score=50)[:limit]


def search_recordings(
    query: str,
    title: Optional[str] = None,
    artist: Optional[str] = None,
    limit: int = 15,
    fast: bool = False,
) -> tuple[list[SearchResult], list[ArtistResult]]:
    """Search recordings and artists in parallel. Returns (results, artists)."""

    # If explicit title+artist given, skip artist search
    if title and artist:
        return _search_recordings_only(query, title, artist, limit, fast), []

    # Fire recording search and artist search in parallel
    with ThreadPoolExecutor(max_workers=2) as pool:
        rec_future = pool.submit(_search_recordings_only, query, title, artist, limit, fast)
        artist_future = pool.submit(_search_artist, query)

    return rec_future.result(), artist_future.result()
