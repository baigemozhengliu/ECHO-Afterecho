"""Portable playlist validation and conservative metadata matching."""

from __future__ import annotations

import unicodedata
from typing import Any
from opencc import OpenCC

_to_simplified = OpenCC("t2s")


def clean_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    result = value.strip()
    if len(result) > 500:
        raise ValueError(f"{field} is too long")
    return result


def normalize_track(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("track must be an object")
    artists = value.get("artists")
    if not isinstance(artists, list) or not 1 <= len(artists) <= 20:
        raise ValueError("artists must be a nonempty array")
    track: dict[str, Any] = {
        "title": clean_text(value.get("title"), "title"),
        "artists": [clean_text(a, "artist") for a in artists],
    }
    for field in ("album", "albumArtist", "isrc"):
        item = value.get(field)
        track[field] = clean_text(item, field) if item is not None else None
    for field in ("trackNumber", "discNumber", "durationMs"):
        item = value.get(field)
        if item is not None and (not isinstance(item, int) or isinstance(item, bool) or item < 1):
            raise ValueError(f"{field} must be a positive integer")
        track[field] = item
    hints = value.get("sourceHints", {})
    if not isinstance(hints, dict) or len(hints) > 20:
        raise ValueError("sourceHints must be a small object")
    track["sourceHints"] = {
        clean_text(k, "source hint key"): clean_text(v, "source hint value")
        for k, v in hints.items()
    }
    return track


def normalize_playlist(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("format") != "echo-portable-library" or value.get("version") != 1:
        raise ValueError("unsupported playlist format or version")
    tracks = value.get("tracks")
    if not isinstance(tracks, list) or len(tracks) > 10000:
        raise ValueError("tracks must be an array of at most 10000 items")
    return {
        "format": "echo-portable-library",
        "version": 1,
        "name": clean_text(value.get("name"), "name"),
        "tracks": [normalize_track(track) for track in tracks],
    }


def comparable(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold()
    text = _to_simplified.convert(text)
    return "".join(character for character in text if character.isalnum())


def artist_matches(wanted_artists: set[str], candidate_artists: set[str]) -> bool:
    if wanted_artists & candidate_artists:
        return True
    # A filename may append a band name or romanized alias to the credited artist.
    # Only accept a substantial prefix; the album and title still have to match.
    return any(len(shorter) >= 4 and longer.startswith(shorter)
               for wanted in wanted_artists for candidate in candidate_artists
               for shorter, longer in ((wanted, candidate) if len(wanted) < len(candidate)
                                       else (candidate, wanted),))


def unknown_artist(artists: list[str]) -> bool:
    return not artists or all(comparable(a) in {'未知艺术家', '未知歌手', 'unknownartist', 'unknown', 'unknownartists', 'unknownsinger'} for a in artists)


def match_score(wanted: dict[str, Any], candidate: dict[str, Any]) -> int:
    """Score 0..100. Missing album/duration cannot earn their points."""
    score = 0
    if comparable(wanted.get("title")) and comparable(wanted.get("title")) == comparable(candidate.get("title")):
        score += 40
    wanted_artists = {comparable(a) for a in wanted.get("artists", [])}
    candidate_artists = {comparable(a) for a in candidate.get("artists", [])}
    title_matches = comparable(wanted.get("title")) == comparable(candidate.get("title"))
    album_matches = bool(wanted.get("album") and comparable(wanted.get("album")) == comparable(candidate.get("album")))
    if wanted_artists and candidate_artists and (wanted_artists & candidate_artists or
                                                (title_matches and album_matches and artist_matches(wanted_artists, candidate_artists))):
        score += 30
    if album_matches:
        score += 20
    # Missing credits are not a contradictory artist. Exact recording title AND
    # album can identify it; callers must reject conflicting artist candidates.
    if title_matches and album_matches and unknown_artist(wanted.get('artists', [])) and not unknown_artist(candidate.get('artists', [])):
        score = max(score, 85)
    if wanted.get("durationMs") and candidate.get("durationMs"):
        if abs(wanted["durationMs"] - candidate["durationMs"]) <= 3000:
            score += 10
    return score
