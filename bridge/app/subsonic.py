"""Read-only Subsonic facade over metadata playlists for ECHO remote indexing."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import re
import logging
import os
from threading import Event, Thread
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import parse_qs
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen
from xml.etree import ElementTree as ET

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .core import comparable, match_score, normalize_track
from .track_metadata import effective_track
from .audio_transport import audio_chunks, byte_window
from .providers import MusicDLSearch
from .source_index import SourceIndex
from .store import PlaylistStore
from .metadata import fetch_metadata, clean_lyric, clean_cover, structured_lines


def song_identity(playlist_id: str, index: int, track: dict[str, Any]) -> str:
    fingerprint = json.dumps(track, ensure_ascii=False, sort_keys=True)
    return "so_" + playlist_id + "_" + str(index) + "_" + hashlib.sha256(fingerprint.encode()).hexdigest()[:12]


def make_subsonic_router(store: PlaylistStore, musicdl: MusicDLSearch, data_dir: Path,
                         services: dict[str, Any] | None = None) -> APIRouter:
    router = APIRouter()
    resolved: dict[str, tuple[float, dict[str, Any] | None]] = {}
    playback_outcomes = {}
    resolve_locks: defaultdict[str, Lock] = defaultdict(Lock)
    logger = logging.getLogger(__name__)
    source_index = SourceIndex(data_dir / "source-index.sqlite")
    only_indexed = os.environ.get("ECHO_PORTABLE_ONLY_INDEXED", "0") == "1"
    cache_dir = data_dir / "audio-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    prefetch_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="music-prefetch")
    prefetch_pending: set[str] = set()
    prefetch_lock = Lock()
    metadata_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="music-metadata")
    metadata_jobs = {}
    metadata_lock = Lock()
    artwork_dir = data_dir / "artwork-cache"
    artwork_dir.mkdir(parents=True, exist_ok=True)
    stop_indexer = Event()
    playback_activity = [time.monotonic()]
    playback_queue: dict[str, Any] = {}
    catalog_cache: tuple[list[dict[str, Any]], dict[str, dict], dict[str, dict], dict[str, dict]] | None = None
    catalog_cached_at = 0.0
    catalog_revision = 0
    audio_types = {"flac": "audio/flac", "mp3": "audio/mpeg", "m4a": "audio/mp4",
                   "aac": "audio/aac", "ogg": "audio/ogg"}

    identity_revisions = {}

    def invalidate_track(identity):
        nonlocal catalog_cache
        catalog_cache = None
        identity_revisions[identity] = identity_revisions.get(identity, 0) + 1
        resolved.pop(identity, None)
        source_index.invalidate(identity)
        for suffix in audio_types:
            try:
                (cache_dir / f'{identity}.{suffix}').unlink(missing_ok=True)
            except OSError:
                pass
    store.on_change = invalidate_track

    def cached_audio(song_id: str) -> Path | None:
        for suffix in audio_types:
            path = cache_dir / f"{song_id}.{suffix}"
            if path.is_file():
                path.touch()
                return path
        return None

    def trim_audio_cache() -> None:
        files = sorted((path for path in cache_dir.iterdir() if path.is_file() and
                        path.suffix[1:] in audio_types), key=lambda path: path.stat().st_mtime)
        total = sum(path.stat().st_size for path in files)
        while files and (total > 512_000_000 or len(files) > 32):
            victim = files.pop(0)
            try:
                size = victim.stat().st_size
                victim.unlink()
                total -= size
            except OSError:
                break

    def prefetch_audio(song_id: str, songs: dict[str, dict]) -> None:
        try:
            if cached_audio(song_id):
                return
            source = source_for(song_id, songs)
            if not source:
                return
            suffix = source.get("format")
            if suffix not in audio_types:
                return
            destination = cache_dir / f"{song_id}.{suffix}"
            partial = cache_dir / f"{song_id}.{suffix}.part"
            try:
                with urlopen(UrlRequest(source["url"], headers={"User-Agent": "Afterecho/0.1"}), timeout=10) as upstream:
                    if upstream.headers.get_content_type() not in {*audio_types.values(), "audio/x-flac", "application/octet-stream"}:
                        return
                    size = 0
                    expected_length = upstream.headers.get("Content-Length")
                    with partial.open("wb") as output:
                        while block := upstream.read(262144):
                            size += len(block)
                            if size > 80_000_000:
                                return
                            output.write(block)
                if size and expected_length is not None and size == int(expected_length):
                    partial.replace(destination)
                    trim_audio_cache()
                    logger.info("Prefetched song %s (%d bytes)", song_id, size)
            except (OSError, ValueError) as exc:
                logger.info("Prefetch unavailable for song %s: %s", song_id, type(exc).__name__)
            finally:
                partial.unlink(missing_ok=True)
        finally:
            with prefetch_lock:
                prefetch_pending.discard(song_id)

    def queue_prefetch(song_ids: list[str], songs: dict[str, dict]) -> None:
        for song_id in song_ids:
            with prefetch_lock:
                if song_id in prefetch_pending or cached_audio(song_id):
                    continue
                if len(prefetch_pending) >= 4:
                    return
                prefetch_pending.add(song_id)
            prefetch_pool.submit(prefetch_audio, song_id, songs)

    def next_playlist_songs(song_id: str, songs: dict[str, dict]) -> list[str]:
        if playback_queue.get("current") == song_id and time.monotonic() - playback_queue.get("at", 0) < 600:
            return [identity for identity in playback_queue.get("next", []) if identity in songs]
        folder_id = songs[song_id]["public"]["musicFolderId"]
        song_ids = [key for key, item in songs.items() if item["public"]["musicFolderId"] == folder_id]
        try:
            offset = song_ids.index(song_id)
        except ValueError:
            return []
        return song_ids[offset + 1:offset + 3]
    auth_path = data_dir / "subsonic-auth.json"
    if auth_path.exists():
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
    else:
        auth = {"username": "echo-portable", "password": secrets.token_urlsafe(18)}
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        auth_path.write_text(json.dumps(auth, ensure_ascii=False, indent=2), encoding="utf-8")

    def authorize(params: dict[str, str]) -> None:
        if not hmac.compare_digest(params.get("u", ""), auth["username"]):
            raise HTTPException(401, "invalid Subsonic credentials")
        supplied = params.get("p", "")
        if supplied.startswith("enc:"):
            try:
                supplied = bytes.fromhex(supplied[4:]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                supplied = ""
        token, salt = params.get("t"), params.get("s")
        token_ok = bool(token and salt and hmac.compare_digest(token, hashlib.md5((auth["password"] + salt).encode()).hexdigest()))
        if not token_ok and not hmac.compare_digest(supplied, auth["password"]):
            raise HTTPException(401, "invalid Subsonic credentials")

    def catalog() -> tuple[list[dict[str, Any]], dict[str, dict], dict[str, dict], dict[str, dict]]:
        nonlocal catalog_cache, catalog_cached_at, catalog_revision
        revision = store.path.stat().st_mtime_ns
        if catalog_cache is not None and revision == catalog_revision and time.monotonic() - catalog_cached_at < 20:
            return catalog_cache
        playlists = store.mounted()
        indexed = source_index.all()
        artists: dict[str, dict] = {}
        albums: dict[str, dict] = {}
        songs: dict[str, dict] = {}
        for playlist in playlists:
            identities = store.song_ids(playlist)
            for track_index, track in enumerate(playlist["tracks"]):
                song_id = identities[track_index]
                track = effective_track(track, indexed.get(song_id))
                artist_name = ", ".join(track["artists"])
                artist_id = "ar_" + hashlib.sha256(artist_name.encode()).hexdigest()[:16]
                album_name = track.get("album") or "单曲"
                album_artist = track.get("albumArtist") or artist_name
                album_key = track.get("album") or f"single:{track_index}"
                album_id = "al_" + hashlib.sha256((playlist["id"] + "\0" + album_key + "\0" + (track.get("albumArtist") or "")).encode()).hexdigest()[:16]
                if only_indexed and (indexed.get(song_id) or {}).get("status") != "ready":
                    continue
                artists.setdefault(artist_id, {"id": artist_id, "name": artist_name, "album": []})
                album = albums.setdefault(album_id, {"id": album_id, "name": album_name, "artist": album_artist,
                                                      "artistId": artist_id, "musicFolderId": playlist["id"],
                                                      "song": [], "_artists": set()})
                album["_artists"].add(artist_name)
                if not any(item["id"] == album_id for item in artists[artist_id]["album"]):
                    artists[artist_id]["album"].append({"id": album_id, "name": album_name})
                item = {"id": song_id, "parent": album_id, "title": track["title"], "album": album_name,
                        "artist": artist_name, "artistId": artist_id, "albumId": album_id,
                        "musicFolderId": playlist["id"], "isDir": False, "isVideo": False,
                        "duration": round((track.get("durationMs") or 0) / 1000), "type": "music",
                        "path": f"{playlist['name']}/{album_name}/{track['title']}.mp3"}
                item["coverArt"] = album_id
                if track.get("trackNumber"):
                    item["track"] = track["trackNumber"]
                if track.get("discNumber"):
                    item["discNumber"] = track["discNumber"]
                item = public_song(item, song_id, indexed.get(song_id, {}))
                songs.setdefault(song_id, {"public": item, "track": track, "raw_track": playlist["tracks"][track_index]})
                album["song"].append(item)
        for album in albums.values():
            if len(album["_artists"]) > 1 and not any(song.get("albumArtist") for song in
                                                        (songs[item["id"]]["track"] for item in album["song"])):
                album["artist"] = "Various Artists"
            for item in album["song"]:
                item["albumArtist"] = album["artist"]
            del album["_artists"]
            album["songCount"] = len(album["song"])
            album["duration"] = sum(song["duration"] for song in album["song"])
        for artist in artists.values():
            artist["albumCount"] = len(artist["album"])
        catalog_cache = (playlists, artists, albums, songs)
        catalog_cached_at = time.monotonic()
        catalog_revision = revision
        return catalog_cache

    def envelope(data: dict[str, Any], fmt: str) -> Response:
        payload = {"status": "ok", "version": "1.16.1", "type": "echo-portable-library", "serverVersion": "0.1.12", **data}
        if fmt == "json":
            return JSONResponse({"subsonic-response": payload})
        root = ET.Element("subsonic-response", xmlns="http://subsonic.org/restapi")
        def add(parent: ET.Element, name: str, value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    add(parent, name, item)
                return
            child = ET.SubElement(parent, name)
            if isinstance(value, dict):
                for key, item in value.items():
                    if isinstance(item, (list, dict)):
                        add(child, key, item)
                    elif item is not None:
                        child.set(key, str(item).lower() if isinstance(item, bool) else str(item))
            elif value is not None:
                child.text = str(value)
        for key, value in payload.items():
            if isinstance(value, (list, dict)):
                add(root, key, value)
            else:
                root.set(key, str(value))
        return Response(ET.tostring(root, encoding="utf-8", xml_declaration=True), media_type="application/xml")

    def source_for(song_id: str, songs: dict[str, dict]) -> dict[str, Any] | None:
        with resolve_locks[song_id]:
            identity_revision = identity_revisions.get(song_id, 0)
            cached = resolved.get(song_id)
            if cached is not None and time.monotonic() - cached[0] < (600 if cached[1] else 10):
                if cached[1]:
                    # Keep the same representation while FFmpeg probes and seeks.
                    resolved[song_id] = (time.monotonic(), cached[1])
                return cached[1]
            recent_url = source_index.recent_url(song_id)
            indexed = source_index.get(song_id)
            if recent_url and indexed and indexed.get("status") == "ready":
                source = {**indexed, "url": recent_url}
                resolved[song_id] = (time.monotonic(), source)
                return source
            failed = False
            try:
                source = musicdl.refresh(indexed) if indexed and indexed.get("sourceId") else None
                if not source:
                    wanted = songs[song_id]["track"]
                    if indexed and indexed.get("provider"):
                        wanted = {**wanted, "sourceHints": {**wanted.get("sourceHints", {}),
                                                            "preferredProvider": indexed["provider"]}}
                    source = musicdl.resolve(wanted)
            except TimeoutError:
                logger.info("Source search timed out or busy for song %s", song_id)
                source = None
                failed = True
            except Exception:
                logger.exception("MusicDL resolution failed for song %s", song_id)
                source = None
                failed = True
            if source:
                candidate = source
                for validation_attempt in range(2):
                    source = candidate
                    try:
                        with urlopen(UrlRequest(source["url"], headers={"Range": "bytes=0-127",
                                                                      "User-Agent": "Afterecho/0.1"}), timeout=3) as upstream:
                            content_type = upstream.headers.get_content_type()
                            signature = upstream.read(64)
                            total = re.search(r"/(\d+)$", upstream.headers.get("Content-Range", ""))
                            if total:
                                source["fileSizeBytes"] = int(total.group(1))
                        if signature[:4] == b"fLaC" and len(signature) >= 42 and signature[4] & 0x7f == 0:
                            packed = int.from_bytes(signature[18:26], "big")
                            sample_rate = (packed >> 44) & 0xfffff
                            if sample_rate:
                                source["sampleRate"] = sample_rate
                                source["bitDepth"] = ((packed >> 36) & 0x1f) + 1
                                source["durationMs"] = round((packed & ((1 << 36) - 1)) * 1000 / sample_rate)
                        audio_signature = (signature.startswith((b"fLaC", b"ID3", b"OggS")) or
                                           signature[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2") or
                                           b"ftyp" in signature[:12])
                        if content_type not in {*audio_types.values(), "audio/x-flac", "application/octet-stream"} or not signature:
                            source = None
                        elif content_type == "application/octet-stream" and not audio_signature:
                            source = None
                        failed = False
                        break
                    except (HTTPError, URLError, TimeoutError, OSError) as exc:
                        logger.info('Audio validation transient failure song=%s error=%s', song_id, type(exc).__name__)
                        source = None
                        failed = True
            if identity_revision != identity_revisions.get(song_id, 0):
                return None
            resolved[song_id] = (time.monotonic(), source)
            if not failed:
                source_index.set(song_id, source)
            return source

    def public_song(item: dict[str, Any], song_id: str, indexed: dict[str, Any] | None = None) -> dict[str, Any]:
        source = resolved.get(song_id)
        details = {**(indexed if indexed is not None else source_index.get(song_id) or {}),
                   **(source[1] if source and source[1] else {})}
        if not details:
            return item
        enriched = dict(item)
        if details.get("durationMs"):
            enriched["duration"] = round(details["durationMs"] / 1000)
        if details.get("format"):
            enriched["suffix"] = details["format"]
            enriched["contentType"] = {"flac": "audio/flac", "mp3": "audio/mpeg", "m4a": "audio/mp4"}.get(details["format"], "application/octet-stream")
        if details.get("sampleRate"):
            enriched["samplingRate"] = details["sampleRate"]
        if details.get("bitDepth"):
            enriched["bitDepth"] = details["bitDepth"]
        if details.get("fileSizeBytes"):
            enriched["size"] = details["fileSizeBytes"]
            if details.get("durationMs"):
                enriched["bitRate"] = round(details["fileSizeBytes"] * 8 / details["durationMs"])
        enriched["sourceProvider"] = details.get("provider")
        return enriched

    def metadata_for(song_id: str, songs: dict[str, dict], wait: float = 0) -> dict:
        source = source_index.get(song_id) or {}
        saved = source_index.metadata(song_id)
        details = {**source, 'metadataCheckedAt': saved.get('metadataCheckedAt', 0)}
        details['lyric'] = clean_lyric(details.get('lyric'))
        details['coverUrl'] = clean_cover(details.get('coverUrl'))
        if details.get('lyric') and details.get('coverUrl') and details.get('durationMs'):
            return details
        if time.time() - details.get('metadataCheckedAt', 0) < 300:
            return details
        metadata_revision = identity_revisions.get(song_id, 0)
        def retrieve():
            result = fetch_metadata(musicdl, songs[song_id]['track'], details)
            if metadata_revision != identity_revisions.get(song_id, 0):
                return source_index.get(song_id) or {}
            source_index.set_metadata(song_id, result)
            return {**details, **result}
        with metadata_lock:
            for identity, job in list(metadata_jobs.items()):
                if job.done():
                    del metadata_jobs[identity]
            job = metadata_jobs.get(song_id)
            if job is None and len(metadata_jobs) < 4:
                job = metadata_pool.submit(retrieve)
                metadata_jobs[song_id] = job
        if job is not None and wait:
            try:
                result = job.result(timeout=wait)
                if isinstance(result, dict):
                    return result
            except Exception:
                pass
        return details

    def match_imported(track: dict[str, Any], songs: dict[str, dict]) -> str | None:
        title = comparable(track.get("title", ""))
        candidates = [(max(match_score(track, variant) for variant in (item['track'], item.get('raw_track', item['track']))), identity)
                      for identity, item in songs.items()
                      if any(comparable(variant['title']) == title for variant in (item['track'], item.get('raw_track', item['track'])))]
        if not candidates:
            return None
        score, identity = max(candidates)
        if score < (85 if track.get("album") else 70):
            return None
        return identity

    def resolve_imported(track: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
        # Panel/lyrics/cover jobs and Subsonic playback must share a single resolver.
        _, _, _, songs = catalog()
        identity = match_imported(track, songs)
        return (True, source_for(identity, songs)) if identity else (False, None)

    if services is not None:
        services["resolve_imported"] = resolve_imported

    @router.post("/metadata/lookup")
    def metadata_lookup(body: dict[str, Any]) -> dict[str, Any]:
        try:
            track = normalize_track(body.get("track"))
        except ValueError as exc:
            raise HTTPException(400, "invalid track metadata") from exc
        _, _, _, songs = catalog()
        identity = match_imported(track, songs)
        details = metadata_for(identity, songs, wait=6) if identity else None
        result = {key: details.get(key) for key in ("provider", "matchScore", "coverUrl", "lyric")} if details and any(details.get(key) for key in ('provider', 'coverUrl', 'lyric')) else None
        if result and result.get('coverUrl'):
            result['coverUrl'] = f'http://127.0.0.1:18765/artwork/{identity}'
        return {"source": result}

    @router.post('/playback/recovery')
    def playback_recovery(body: dict) -> dict:
        _, _, _, songs = catalog()
        host_id = body.get('hostTrackId', '')
        # ECHO's Subsonic stable key is the song id; only an exact host-id hash
        # match can trigger automatic controls, never a title-only match.
        identity = next((sid for sid in songs if isinstance(host_id, str)
                         and host_id.startswith('remote:')
                         and host_id.endswith(':' + hashlib.sha1(sid.encode()).hexdigest()[:32])), None)
        outcome = playback_outcomes.get(identity)
        if not outcome or time.monotonic() - outcome['at'] > 90:
            return {'owned': bool(identity), 'failed': False}
        if body.get('retry') and outcome['failed']:
            resolved.pop(identity, None)
            source_index.forget_url(identity)
        return {'owned': True, 'failed': outcome['failed']}

    @router.post("/playback/queue")
    def update_playback_queue(body: dict[str, Any]) -> dict[str, Any]:
        tracks = body.get("tracks", [])
        if not isinstance(tracks, list) or len(tracks) > 2:
            raise HTTPException(400, "queue prefetch accepts at most two tracks")
        try:
            tracks = [normalize_track(track) for track in tracks]
            current = normalize_track(body["current"]) if body.get("current") else None
        except ValueError as exc:
            raise HTTPException(400, "invalid queue track") from exc
        _, _, _, songs = catalog()
        identities = list(dict.fromkeys(identity for track in tracks if (identity := match_imported(track, songs))))
        playback_activity[0] = time.monotonic()
        playback_queue.update(current=match_imported(current, songs) if current else None,
                              next=identities, at=time.monotonic())
        queue_prefetch(identities, songs)
        return {"queued": len(identities)}

    def index_loop() -> None:
        while not stop_indexer.is_set():
            known = source_index.all()
            for playlist in store.mounted():
                identities = store.song_ids(playlist)
                for index, track in enumerate(playlist["tracks"]):
                    if stop_indexer.is_set():
                        return
                    song_id = identities[index]
                    entry = known.get(song_id)
                    age = time.time() - entry["checkedAt"] if entry else float("inf")
                    if entry and age < (86400 if entry["status"] == "unavailable" else 604800):
                        continue
                    while time.monotonic() - playback_activity[0] < 120:
                        if stop_indexer.wait(10):
                            return
                    source_for(song_id, {song_id: {"track": effective_track(track, entry)}})
                    if stop_indexer.wait(30):
                        return
            stop_indexer.wait(120)

    @router.on_event("startup")
    def begin_indexing() -> None:
        Thread(target=index_loop, name="music-source-index", daemon=True).start()

    @router.on_event("shutdown")
    def end_indexing() -> None:
        stop_indexer.set()
        metadata_pool.shutdown(wait=False, cancel_futures=True)
        prefetch_pool.shutdown(wait=False, cancel_futures=True)

    @router.get("/index/status")
    def index_status() -> dict[str, Any]:
        playlists = store.mounted()
        matches = source_index.counts()
        total = sum(len(playlist["tracks"]) for playlist in playlists)
        return {"playlists": len(playlists), "totalTracks": total, "matches": matches,
                "pending": max(0, total - sum(matches.values())),
                "cachedAudioBytes": sum(path.stat().st_size for path in cache_dir.iterdir()
                                        if path.is_file() and path.suffix[1:] in audio_types)}

    async def cover_art(identity: str, albums: dict, songs: dict) -> Response:
        if identity in albums:
            album_songs = albums[identity]["song"]
            # Covers and lyrics survive restarts and source URL refreshes.
            song_id = next((song["id"] for song in album_songs
                            if (source_index.metadata(song['id']).get('coverUrl') or
                                (source_index.get(song['id']) or {}).get('coverUrl'))),
                           album_songs[0]["id"] if album_songs else "")
        else:
            song_id = identity
        if song_id not in songs:
            raise HTTPException(404, "cover not found")
        source = await run_in_threadpool(metadata_for, song_id, songs, 6)
        cover_url = source.get("coverUrl")
        if not cover_url:
            raise HTTPException(404, "cover not found")
        def fetch_cover() -> tuple[bytes, str]:
            key = hashlib.sha256(cover_url.encode()).hexdigest()
            for ext, mime in [('jpg', 'image/jpeg'), ('png', 'image/png'), ('webp', 'image/webp')]:
                saved = artwork_dir / f'{key}.{ext}'
                if saved.is_file():
                    saved.touch()
                    return saved.read_bytes(), mime
            with urlopen(UrlRequest(cover_url, headers={"User-Agent": "Mozilla/5.0"}), timeout=6) as upstream:
                content_type = upstream.headers.get_content_type()
                content_type = {'image/jpg': 'image/jpeg', 'image/pjpeg': 'image/jpeg',
                                'image/x-png': 'image/png'}.get(content_type, content_type)
                if content_type not in {"image/jpeg", "image/png", "image/webp"}:
                    raise ValueError("invalid cover content type")
                data = upstream.read(5_000_001)
                if len(data) > 5_000_000:
                    raise ValueError("cover too large")
                if not (data.startswith(b'\xff\xd8\xff') or data.startswith(b'\x89PNG\r\n\x1a\n') or
                        (data.startswith(b'RIFF') and data[8:12] == b'WEBP')):
                    raise ValueError('invalid cover bytes')
                extension = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}[content_type]
                partial = artwork_dir / f'{key}.{secrets.token_hex(4)}.part'
                partial.write_bytes(data)
                partial.replace(artwork_dir / f'{key}.{extension}')
                files = sorted(artwork_dir.iterdir(), key=lambda p: p.stat().st_mtime)
                total = sum(p.stat().st_size for p in files)
                for victim in files:
                    if total <= 32_000_000:
                        break
                    size = victim.stat().st_size
                    victim.unlink(missing_ok=True)
                    total -= size
                return data, content_type
        try:
            data, content_type = await run_in_threadpool(fetch_cover)
            return Response(data, media_type=content_type, headers={'Cache-Control': 'private, max-age=86400'})
        except (OSError, ValueError) as exc:
            # Repair stale provider image URLs without refreshing the audio URL.
            refreshed = await run_in_threadpool(fetch_metadata, musicdl, songs[song_id]['track'],
                                                {**source, 'coverUrl': None})
            if refreshed.get('coverUrl') and refreshed['coverUrl'] != cover_url:
                source_index.set_metadata(song_id, refreshed)
                cover_url = refreshed['coverUrl']
                try:
                    data, content_type = await run_in_threadpool(fetch_cover)
                    return Response(data, media_type=content_type, headers={'Cache-Control': 'private, max-age=86400'})
                except (OSError, ValueError):
                    pass
            raise HTTPException(502, "cover unavailable") from exc

    @router.get("/artwork/{song_id}")
    async def public_artwork(song_id: str) -> Response:
        _, _, albums, songs = catalog()
        if song_id not in songs:
            raise HTTPException(404, "cover not found")
        return await cover_art(song_id, albums, songs)

    @router.api_route("/rest/{method}", methods=["GET", "POST", "HEAD"])
    async def subsonic(method: str, request: Request) -> Response:
        params = dict(request.query_params)
        if request.method == "POST":
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
                raise HTTPException(415, "Subsonic POST requires form encoding")
            body = await request.body()
            if len(body) > 65536:
                raise HTTPException(413, "Subsonic request too large")
            params.update({key: values[-1] for key, values in parse_qs(body.decode("utf-8")).items()})
        authorize(params)
        name = method.removesuffix(".view")
        fmt = params.get("f", "xml").lower()
        if name == "ping":
            return envelope({}, fmt)
        if name == "getLicense":
            return envelope({"license": {"valid": True}}, fmt)
        playlists, artists, albums, songs = catalog()
        if name == "getMusicFolders":
            return envelope({"musicFolders": {"musicFolder": [{"id": item["id"], "name": item["name"]} for item in playlists]}}, fmt)
        if name in {"getArtists", "getIndexes"}:
            if name == "getIndexes" and params.get("musicFolderId"):
                folder_id = params["musicFolderId"]
                # Browsing the mounted library is a flat song list. Album APIs
                # remain available for full sync and album/artist navigation.
                entries = [song["public"] for song in songs.values()
                           if song["public"]["musicFolderId"] == folder_id]
                return envelope({"indexes": {"index": [], "child": entries}}, fmt)
            selected = [artist for artist in artists.values() if not params.get("musicFolderId") or
                        any(album["musicFolderId"] == params["musicFolderId"] for album in albums.values()
                            if album["artistId"] == artist["id"])]
            groups: dict[str, list] = defaultdict(list)
            for artist in selected:
                groups[(artist["name"][:1] or "#").upper()].append({key: value for key, value in artist.items() if key != "album"})
            key = "artists" if name == "getArtists" else "indexes"
            return envelope({key: {"index": [{"name": initial, "artist": sorted(items, key=lambda item: item["name"])}
                                              for initial, items in sorted(groups.items())]}}, fmt)
        if name == "getArtist":
            artist = artists.get(params.get("id", ""))
            if artist is None:
                raise HTTPException(404, "artist not found")
            return envelope({"artist": artist}, fmt)
        if name == "getAlbum":
            album = albums.get(params.get("id", ""))
            if album is None:
                raise HTTPException(404, "album not found")
            return envelope({"album": album}, fmt)
        if name == "getSong":
            song = songs.get(params.get("id", ""))
            if song is None:
                raise HTTPException(404, "song not found")
            details = source_index.get(params['id']) or {}
            if not details.get('durationMs') and not song['public'].get('duration'):
                # ECHO refreshes missing duration before it starts FFmpeg. Bounded,
                # metadata-only lookup shares the existing pool; never resolve all
                # audio candidates or block the async server on a network request.
                details = await run_in_threadpool(metadata_for, params['id'], songs, 2)
            item = public_song(song['public'], params['id'], details)
            enriched_track = effective_track(song['track'], details)
            item = {**item, 'artist': ', '.join(enriched_track['artists'])}
            return envelope({'song': item}, fmt)
        if name in {"getAlbumList2", "getAlbumList"}:
            values = list(albums.values())
            if params.get("musicFolderId"):
                values = [album for album in values if album["musicFolderId"] == params["musicFolderId"]]
            values.sort(key=lambda album: (album["name"].casefold(), album["artist"].casefold()))
            start = min(max(int(params.get("offset", 0)), 0), len(values))
            size = min(max(int(params.get("size", 10)), 1), 500)
            items = [{key: value for key, value in album.items() if key != "song"} for album in values[start:start + size]]
            return envelope({"albumList2" if name.endswith("2") else "albumList": {"album": items}}, fmt)
        if name in {"getMusicDirectory"}:
            identity = params.get("id", "")
            folder = next((playlist for playlist in playlists if playlist["id"] == identity), None)
            if folder is not None:
                return envelope({"directory": {"id": identity, "name": folder["name"],
                                                "child": [song["public"] for song in songs.values()
                                                          if song["public"]["musicFolderId"] == identity]}}, fmt)
            if identity in albums:
                queue_prefetch([song["id"] for song in albums[identity]["song"][:2]], songs)
                return envelope({"directory": {"id": identity, "name": albums[identity]["name"], "child": albums[identity]["song"]}}, fmt)
            if identity in artists:
                return envelope({"directory": {"id": identity, "name": artists[identity]["name"],
                                                "child": [{"id": album["id"], "title": album["name"], "isDir": True}
                                                          for album in albums.values() if album["artistId"] == identity]}}, fmt)
            raise HTTPException(404, "directory not found")
        if name in {"search3", "search2", "search"}:
            query = comparable(params.get("query", ""))
            count = min(max(int(params.get("songCount", 20)), 1), 500)
            offset = max(int(params.get("songOffset", 0)), 0)
            matched = [song["public"] for song in songs.values()
                       if query in comparable(song["public"]["title"] + song["public"]["artist"] + song["public"]["album"])]
            return envelope({"searchResult3": {"song": matched[offset:offset + count]}}, fmt)
        if name == "getPlaylists":
            return envelope({"playlists": {"playlist": [{"id": item["id"], "name": item["name"],
                                                         "songCount": len(item["tracks"])} for item in playlists]}}, fmt)
        if name == "getPlaylist":
            playlist = next((item for item in playlists if item["id"] == params.get("id")), None)
            if playlist is None:
                raise HTTPException(404, "playlist not found")
            entries = [song["public"] for song in songs.values() if song["public"]["musicFolderId"] == playlist["id"]]
            return envelope({"playlist": {"id": playlist["id"], "name": playlist["name"], "songCount": len(entries),
                                          "entry": entries}}, fmt)
        if name == "getGenres":
            return envelope({"genres": {"genre": []}}, fmt)
        if name in {"getLyrics", "getLyricsBySongId"}:
            song_id = params.get("id", "")
            if name == "getLyrics":
                title = comparable(params.get("title", ""))
                artist = comparable(params.get("artist", ""))
                song_id = next((key for key, value in songs.items()
                                if comparable(value["public"]["title"]) == title
                                and comparable(value["public"]["artist"]) == artist), "")
            if song_id not in songs:
                raise HTTPException(404, "song not found")
            source = await run_in_threadpool(metadata_for, song_id, songs, 6)
            lyric = source.get("lyric") or ""
            if name == "getLyricsBySongId":
                lines = structured_lines(lyric)
                return envelope({"lyricsList": {"structuredLyrics": [{"displayArtist": songs[song_id]["public"]["artist"],
                                                                       "displayTitle": songs[song_id]["public"]["title"],
                                                                       "synced": any("start" in line for line in lines),
                                                                       "line": lines}]}}, fmt)
            return envelope({"lyrics": {"artist": songs[song_id]["public"]["artist"],
                                         "title": songs[song_id]["public"]["title"], "value": lyric}}, fmt)
        if name == "getCoverArt":
            return await cover_art(params.get("id", ""), albums, songs)
        if name in {"stream", "download"}:
            playback_activity[0] = time.monotonic()
            playback_started = time.monotonic()
            song = songs.get(params.get("id", ""))
            if song is None:
                raise HTTPException(404, "song not found")
            song_id = params["id"]
            following = next_playlist_songs(song_id, songs)
            local_audio = cached_audio(song_id)
            if local_audio is not None:
                logger.info("Audio cache hit song=%s range=%s", song_id, request.headers.get("range", "full"))
                queue_prefetch(following, songs)
                return FileResponse(local_audio, media_type=audio_types[local_audio.suffix[1:]])
            headers = {"User-Agent": "Afterecho/0.1"}
            if request.headers.get("range"):
                headers["Range"] = request.headers["range"]
            upstream = None
            source = None
            for attempt in range(2):
                source = await run_in_threadpool(source_for, song_id, songs)
                if source is None:
                    break
                try:
                    upstream = await run_in_threadpool(
                        lambda: urlopen(UrlRequest(source["url"], headers=headers), timeout=8))
                    break
                except (HTTPError, URLError, TimeoutError, OSError) as exc:
                    logger.warning("Audio upstream attempt %d failed for song %s from %s: %s",
                                   attempt + 1, song_id, source.get("provider"), type(exc).__name__)
                    resolved.pop(song_id, None)
                    source_index.forget_url(song_id)
            if await request.is_disconnected():
                if upstream is not None:
                    upstream.close()
                return Response(status_code=499)
            if upstream is None:
                playback_outcomes[song_id] = {'at': time.monotonic(), 'failed': True}
                logger.info("No playable source for song %s", song_id)
                raise HTTPException(502, "audio source unavailable")
            playback_outcomes[song_id] = {'at': time.monotonic(), 'failed': False}
            content_type = upstream.headers.get_content_type()
            if content_type not in {"audio/mpeg", "audio/flac", "audio/x-flac", "audio/mp4", "audio/aac", "audio/ogg", "application/octet-stream"}:
                upstream.close()
                raise HTTPException(502, "audio source returned unsupported content")
            if content_type == "audio/x-flac":
                content_type = "audio/flac"
            if content_type == "application/octet-stream":
                suffix = source["url"].split("?", 1)[0].rsplit(".", 1)[-1].lower()
                content_type = audio_types.get(suffix, content_type)
            response_headers = {key: value for key in ("Content-Length", "Content-Range", "Accept-Ranges")
                                if (value := upstream.headers.get(key)) is not None}
            response_headers["Cache-Control"] = "no-store"
            if request.method == "HEAD":
                upstream.close()
                return Response(status_code=upstream.status, media_type=content_type, headers=response_headers)
            try:
                range_start, range_end, total_size = byte_window(upstream)
                requested_start = re.match(r'^bytes=(\d+)-', request.headers.get('range', ''))
                if requested_start and upstream.status == 206 and range_start != int(requested_start[1]):
                    raise OSError('upstream returned the wrong requested audio range')
            except (OSError, ValueError) as exc:
                upstream.close()
                raise HTTPException(502, "invalid upstream audio range") from exc
            queue_prefetch(following, songs)
            def chunks():
                suffix = source.get("format")
                save_audio = (range_start == 0 and total_size is not None and 0 < total_size <= 80_000_000
                              and range_end == total_size - 1 and suffix in audio_types)
                partial = cache_dir / f"{song_id}.{secrets.token_hex(6)}.stream.part"
                output = None
                written = 0
                first_block = True
                def reopen(offset, end):
                    logger.info("Resuming audio for song %s at byte %d", song_id, offset)
                    retry_headers = {**headers, "Range": f"bytes={offset}-{end if end is not None else ''}"}
                    return urlopen(UrlRequest(source["url"], headers=retry_headers), timeout=8)
                try:
                    if save_audio:
                        try:
                            output = partial.open("wb")
                        except OSError:
                            output = None
                    for data in audio_chunks(upstream, reopen):
                        playback_activity[0] = time.monotonic()
                        if first_block:
                            logger.info("Audio first bytes song=%s provider=%s elapsed_ms=%d range=%s",
                                        song_id, source.get("provider"),
                                        round((time.monotonic() - playback_started) * 1000),
                                        request.headers.get("range", "full"))
                            first_block = False
                        if output is not None:
                            try:
                                output.write(data)
                                written += len(data)
                            except OSError:
                                output.close()
                                output = None
                        yield data
                    if output is not None:
                        output.close()
                        output = None
                        if written == total_size:
                            try:
                                partial.replace(cache_dir / f"{song_id}.{suffix}")
                                trim_audio_cache()
                            except OSError:
                                pass
                except (OSError, ValueError) as exc:
                    playback_outcomes[song_id] = {"at": time.monotonic(), "failed": True}
                    logger.warning("Audio transfer failed for song %s: %s", song_id, type(exc).__name__)
                    raise
                finally:
                    if output is not None:
                        output.close()
                    upstream.close()
                    partial.unlink(missing_ok=True)
            return StreamingResponse(chunks(), status_code=upstream.status, media_type=content_type, headers=response_headers)
        raise HTTPException(404, "unsupported Subsonic method")

    return router
