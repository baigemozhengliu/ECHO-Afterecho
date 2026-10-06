"""Loopback-only HTTP bridge for portable playlists."""

from __future__ import annotations

import os
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, Future
from threading import Lock
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from .core import match_score, normalize_playlist, normalize_track
from .providers import DirectCatalog, MusicDLSearch
from .store import PlaylistStore
from .subsonic import make_subsonic_router
from .track_metadata import effective_track
from .source_index import SourceIndex
from .metadata import fetch_metadata
from .discovery import search_candidates
import uuid


def make_app(data_dir: Path | None = None) -> FastAPI:
    data_dir = data_dir or Path(os.environ.get("ECHO_PORTABLE_DATA", Path.cwd() / "data"))
    store = PlaylistStore(data_dir / "playlists.sqlite")
    if not store.list():
        default = store.save({'format':'echo-portable-library','version':1,'name':'默认歌单','tracks':[]})
        store.activate(default['id'])
    catalog = DirectCatalog(data_dir / "authorized-catalog.json")
    sources = [value for value in os.environ.get("ECHO_MUSICDL_SOURCES", "").split(",") if value]
    musicdl = MusicDLSearch(sources, data_dir / "musicdl-work")
    musicdl.warmup()
    app = FastAPI(title="Afterecho Bridge", version="0.2.0")
    library_services: dict[str, Any] = {}
    app.include_router(make_subsonic_router(store, musicdl, data_dir, library_services))
    resolver_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="musicdl-resolve")
    resolve_jobs: dict[str, tuple[float, Future]] = {}
    jobs_lock = Lock()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": "0.2.0", "service": "echo-portable-library", "dataDir": str(data_dir.resolve()), "catalogTracks": len(catalog.entries()), "musicdlSources": sources}

    @app.get('/mount')
    def mount_status():
        return {'activePlaylistId': store.active_id()}

    @app.post('/mount')
    def activate_playlist(body: dict):
        try:
            store.activate(body.get('playlistId'))
            return {'activePlaylistId': store.active_id(), 'requiresRescan': True}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post('/library/{playlist_id}/view')
    def library_view(playlist_id: str, body: dict):
        playlist = store.get(playlist_id) or _not_found()
        identities = store.song_ids(playlist)
        known = SourceIndex(data_dir / 'source-index.sqlite').all()
        originals = store.originals()
        query = str(body.get('query', '')).casefold()
        rows = []
        for position, (identity, raw) in enumerate(zip(identities, playlist['tracks'])):
            details = known.get(identity) or {}
            track = effective_track(raw, details)
            if query and query not in (track['title'] + ' ' + ' '.join(track['artists']) + ' ' + (track.get('album') or '')).casefold():
                continue
            state = 'matched' if details.get('sourceId') else 'review'
            if details.get('status') == 'unavailable':
                state = 'unavailable'
            if body.get('state') and body['state'] != state:
                continue
            rows.append({'index': position, 'identity': identity, 'track': track, 'original': originals.get(identity, raw),
                         'state': state, 'provider': details.get('provider'), 'sourceId': details.get('sourceId')})
        if body.get('group') in ('album', 'artist'):
            field = body['group']
            groups = {}
            for row in rows:
                values = row['track']['artists'] if field == 'artist' else [row['track'].get('album') or '单曲']
                for value in values:
                    groups.setdefault(value, []).append(row['identity'])
            return {'groups': [{'name': k, 'count': len(v), 'identities': v} for k,v in sorted(groups.items())], 'total': len(rows)}
        if body.get('selectionOnly'):
            return {'identities': [r['identity'] for r in rows]}
        page = max(1, int(body.get('page', 1)))
        return {'rows': rows[(page-1)*50:page*50], 'total': len(rows), 'page': page, 'hasMore': page*50 < len(rows)}

    preparation_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='library-prepare')
    preparation = {'running': False}
    preparation_lock = Lock()

    @app.get('/library/{playlist_id}/prepare')
    def preparation_status(playlist_id: str):
        return dict(preparation) if preparation.get('playlistId') == playlist_id else {'running': False}

    @app.post('/library/{playlist_id}/prepare')
    def prepare_playlist(playlist_id: str, body: dict):
        playlist = store.get(playlist_id) or _not_found()
        if body.get('stop'):
            if preparation.get('playlistId') == playlist_id:
                preparation['stop'] = True
            return dict(preparation)
        with preparation_lock:
            if preparation['running']:
                raise HTTPException(409, '已有歌单正在预解析，请先停止或等待完成')
            preparation.clear()
            preparation.update(running=True, stop=False, playlistId=playlist_id, total=len(playlist['tracks']), checked=0, matched=0)
        identities = store.song_ids(playlist)
        def run():
            index = SourceIndex(data_dir / 'source-index.sqlite')
            try:
                for identity, raw in zip(identities, playlist['tracks']):
                    if preparation.get('stop'):
                        break
                    saved = index.get(identity) or {}
                    if saved.get('sourceId'):
                        preparation['matched'] += 1
                    else:
                        tags = fetch_metadata(musicdl, effective_track(raw, saved), saved, identity_only=True)
                        latest = store.get(playlist_id)
                        if latest is None:
                            break
                        latest_ids = store.song_ids(latest)
                        if identity not in latest_ids or latest['tracks'][latest_ids.index(identity)] != raw:
                            preparation['checked'] += 1
                            continue
                        if tags:
                            index.set_metadata(identity, tags)
                        if tags.get('sourceId'):
                            preparation['matched'] += 1
                        time.sleep(1)
                    preparation['checked'] += 1
            finally:
                preparation['running'] = False
        preparation_pool.submit(run)
        return dict(preparation)

    @app.post('/library/{playlist_id}/inspect')
    def inspect_track(playlist_id: str, body: dict):
        playlist = store.get(playlist_id) or _not_found()
        position = body.get('index')
        if body.get('identity'):
            ids = store.song_ids(playlist)
            position = ids.index(body['identity']) if body['identity'] in ids else None
        if not isinstance(position, int) or not 0 <= position < len(playlist['tracks']):
            raise HTTPException(400, 'invalid track index')
        identity = store.song_ids(playlist)[position]
        index = SourceIndex(data_dir / 'source-index.sqlite')
        source = index.get(identity) or {}
        track = effective_track(playlist['tracks'][position], source)
        tags = fetch_metadata(musicdl, track, source)
        latest = store.get(playlist_id)
        latest_ids = store.song_ids(latest) if latest else []
        if identity not in latest_ids or latest['tracks'][latest_ids.index(identity)] != playlist['tracks'][position]:
            raise HTTPException(409, '查询期间歌曲已修改，请重新检查')
        index.set_metadata(identity, tags)
        return {'track': effective_track(track, index.get(identity)), 'matched': bool(tags.get('matchScore'))}

    discovery_results = {}
    discovery_lock = Lock()

    @app.post('/discovery/search')
    def discover(body: dict):
        query = body.get('query')
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
            raise HTTPException(400, '请输入 1–200 字检索词')
        try:
            result = search_candidates(musicdl, query.strip(), body.get('provider', 'all'))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        with discovery_lock:
            for key, (expires, _) in list(discovery_results.items()):
                if expires < time.monotonic(): del discovery_results[key]
            for item in result['candidates']:
                token = uuid.uuid4().hex
                discovery_results[token] = (time.monotonic()+900, item.copy())
                item['token'] = token
        return result

    @app.post('/library/{playlist_id}/bind')
    def bind_candidate(playlist_id: str, body: dict):
        with discovery_lock:
            entry = discovery_results.get(body.get('token'))
        if not entry or entry[0] < time.monotonic():
            raise HTTPException(409, '候选已过期，请重新搜索')
        candidate = entry[1]; track = candidate['track']
        playlist = store.get(playlist_id) or _not_found()
        identity = body.get('identity')
        if identity:
            ids = store.song_ids(playlist)
            if identity not in ids: raise HTTPException(409, '歌曲已变更，请重新选择')
            position = ids.index(identity)
            # User confirms this recording; retain original input in original_tracks.
            store.edit_track(playlist_id, position, {k:track[k] for k in ('title','artists','album','durationMs','sourceHints')})
            if store.on_change: store.on_change(identity)
        else:
            existing = next((i for i,t in enumerate(playlist['tracks']) if t.get('sourceHints', {}).get(candidate['provider']) == candidate['sourceId']), None)
            if existing is not None:
                return {'identity':store.song_ids(playlist)[existing], 'track':playlist['tracks'][existing], 'existing':True}
            playlist = store.add_track(playlist_id, track)
            identity = store.song_ids(playlist)[-1]
        SourceIndex(data_dir / 'source-index.sqlite').set_metadata(identity, {
            'provider':candidate['provider'], 'sourceId':candidate['sourceId'], 'title':track['title'],
            'artists':track['artists'], 'artist':', '.join(track['artists']), 'album':track.get('album'),
            'durationMs':track.get('durationMs'), 'matchScore':100})
        return {'identity':identity, 'track':track}

    @app.post('/library/{playlist_id}/edit')
    def edit_identity(playlist_id: str, body: dict):
        playlist = store.get(playlist_id) or _not_found()
        ids = store.song_ids(playlist)
        if body.get('identity') not in ids: raise HTTPException(409, '歌曲已变更，请刷新')
        try:
            result = store.edit_track(playlist_id, ids.index(body['identity']), {k:v for k,v in body.items() if k != 'identity'})
            return {'saved': True}
        except ValueError as exc: raise HTTPException(400, str(exc))

    @app.post('/library/{playlist_id}/bulk')
    def bulk_edit(playlist_id: str, body: dict):
        try:
            result = store.bulk(playlist_id, body.get('identities'), body.get('action'), body.get('targetId'))
            index = SourceIndex(data_dir / 'source-index.sqlite')
            for old,new in result.get('copied', []):
                if details := index.get(old): index.set_metadata(new, details)
            return {'changed':result['changed']}
        except ValueError as exc: raise HTTPException(400, str(exc))

    @app.post('/playback/telemetry')
    def playback_telemetry(body: dict):
        import logging
        elapsed = body.get('elapsedMs')
        if not isinstance(elapsed, (int, float)) or not 0 <= elapsed <= 300000 or body.get('outcome') not in ('playing', 'error'):
            raise HTTPException(400, 'invalid timing')
        identity = str(body.get('hostTrackId', ''))[:150]
        logging.getLogger('app.playback').info('ECHO state latency track=%s outcome=%s elapsed_ms=%d', identity, body['outcome'], elapsed)
        return {'recorded': True}

    @app.post("/search")
    def search(body: dict[str, Any]) -> dict[str, Any]:
        query = body.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise HTTPException(400, "query must be 1–200 characters")
        tracks = catalog.search(query)
        try:
            tracks.extend(musicdl.search(query))
        except ImportError:
            raise HTTPException(503, "musicdl is configured but not installed")
        return {"tracks": tracks[:100], "total": len(tracks)}

    @app.post("/search-album")
    def search_album(body: dict[str, Any]) -> dict[str, Any]:
        result = search(body)
        albums: dict[tuple[str, str], dict[str, Any]] = {}
        for track in result["tracks"]:
            if not track.get("album"):
                continue
            key = (track["album"].casefold(), track["artists"][0].casefold())
            album = albums.setdefault(key, {"title": track["album"], "artist": track["artists"][0], "tracks": []})
            album["tracks"].append(track)
        for album in albums.values():
            album["tracks"].sort(key=lambda track: (track.get("discNumber") or 1,
                                                     track.get("trackNumber") or 9999, track["title"].casefold()))
        return {"albums": list(albums.values()), "completeTrackLists": False}

    @app.post("/albums/search")
    def search_imported_albums(body: dict[str, Any]) -> dict[str, Any]:
        query = body.get("query", "")
        if not isinstance(query, str) or len(query) > 200:
            raise HTTPException(400, "invalid query")
        term = query.casefold().strip()
        albums: dict[tuple[str, str], dict[str, Any]] = {}
        for playlist in store.list():
            for track in playlist["tracks"]:
                album = track.get("album")
                if not album or term not in f"{album} {' '.join(track['artists'])}".casefold():
                    continue
                key = (playlist["id"], album.casefold())
                group = albums.setdefault(key, {"playlist": playlist["name"], "album": album,
                                                "count": 0, "tracks": []})
                group["count"] += 1
                if len(group["tracks"]) < 50:
                    group["tracks"].append(track)
        return {"albums": list(albums.values())[:30], "total": len(albums)}

    @app.post("/match")
    def match(body: dict[str, Any]) -> dict[str, Any]:
        try:
            track = normalize_track(body.get("track"))
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        candidates = [{"track": item, "score": match_score(track, item)} for item in catalog.search(track["title"])]
        return {"candidates": sorted(candidates, key=lambda item: item["score"], reverse=True)}

    @app.post("/resolve")
    def resolve(body: dict[str, Any]) -> dict[str, Any]:
        try:
            track = normalize_track(body.get("track"))
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        result = catalog.resolve(track)
        if result is None:
            try:
                imported, result = library_services["resolve_imported"](track)
                if not imported:
                    result = musicdl.resolve(track)
            except ImportError as error:
                raise HTTPException(503, "musicdl is configured but not installed") from error
        if result is None:
            raise HTTPException(404, "no reliable authorized playable source")
        return result

    @app.post("/resolve/start")
    def resolve_start(body: dict[str, Any]) -> dict[str, str]:
        try:
            track = normalize_track(body.get("track"))
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        key = hashlib.sha256(json.dumps(track, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        with jobs_lock:
            now = time.monotonic()
            for old_key, (created, future) in list(resolve_jobs.items()):
                if now - created > 60 and future.done():
                    del resolve_jobs[old_key]
            if key not in resolve_jobs or (now - resolve_jobs[key][0] > 60 and resolve_jobs[key][1].done()):
                resolve_jobs[key] = (now, resolver_pool.submit(resolve, {"track": track}))
        return {"jobId": key}

    @app.get("/resolve/status/{job_id}")
    def resolve_status(job_id: str) -> dict[str, Any]:
        if len(job_id) != 64 or any(ch not in "0123456789abcdef" for ch in job_id):
            raise HTTPException(400, "invalid jobId")
        with jobs_lock:
            job = resolve_jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "resolution job expired")
        future = job[1]
        if not future.done():
            return {"state": "pending"}
        try:
            return {"state": "ready", "source": future.result()}
        except HTTPException as error:
            return {"state": "error", "error": error.detail, "status": error.status_code}
        except Exception as error:
            return {"state": "error", "error": str(error), "status": 502}

    def page_items(items: list[dict[str, Any]], body: dict[str, Any]) -> dict[str, Any]:
        try:
            page = int(body.get("page", 1))
            size = int(body.get("pageSize", 24))
        except (TypeError, ValueError) as error:
            raise HTTPException(400, "invalid pagination") from error
        if page < 1 or not 1 <= size <= 25:
            raise HTTPException(400, "invalid pagination")
        start = (page - 1) * size
        return {"tracks": items[start:start + size], "total": len(items),
                "hasMore": start + size < len(items)}

    def track_key(track: dict[str, Any]) -> str:
        encoded = json.dumps(track, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]

    def source_track(playlist: dict[str, Any], track: dict[str, Any]) -> dict[str, Any]:
        return {"providerTrackId": f"{playlist['id']}:{track_key(track)}", "kind": "track", "title": track["title"],
                "artist": ", ".join(track["artists"]), "album": track.get("album"),
                "durationSeconds": track["durationMs"] / 1000 if track.get("durationMs") else None,
                "source": "Afterecho", "playable": True}

    @app.post("/source/search")
    def source_search(body: dict[str, Any]) -> dict[str, Any]:
        query = body.get("query", "")
        if not isinstance(query, str) or len(query) > 200:
            raise HTTPException(400, "invalid query")
        term = query.casefold().strip()
        items = [source_track(playlist, track)
                 for playlist in store.mounted() for track in playlist["tracks"]
                 if term in f"{track['title']} {' '.join(track['artists'])} {track.get('album') or ''}".casefold()]
        return page_items(items, body)

    @app.post("/source/browse")
    def source_browse(body: dict[str, Any]) -> dict[str, Any]:
        items = [{"providerTrackId": playlist["id"], "kind": "collection", "title": playlist["name"],
                  "artist": f"{len(playlist['tracks'])} tracks", "playable": True} for playlist in store.mounted()]
        return page_items(items, body)

    @app.post("/source/collection")
    def source_collection(body: dict[str, Any]) -> dict[str, Any]:
        playlist_id = body.get("collectionId")
        if not isinstance(playlist_id, str):
            raise HTTPException(400, "invalid collectionId")
        playlist = store.get(playlist_id) or _not_found()
        return page_items([source_track(playlist, track) for track in playlist["tracks"]], body)

    @app.post("/source/resolve")
    def source_resolve(body: dict[str, Any]) -> dict[str, Any]:
        provider_track_id = body.get("providerTrackId")
        if not isinstance(provider_track_id, str) or ":" not in provider_track_id:
            raise HTTPException(400, "invalid providerTrackId")
        playlist_id, key = provider_track_id.split(":", 1)
        if len(playlist_id) != 32 or len(key) != 16 or any(ch not in "0123456789abcdef" for ch in playlist_id + key):
            raise HTTPException(400, "invalid providerTrackId")
        playlist = store.get(playlist_id) or _not_found()
        track = next((track for track in playlist["tracks"] if track_key(track) == key), None)
        if track is None:
            _not_found()
        source = resolve({"track": track})
        return {"url": source["url"], "title": track["title"], "artist": ", ".join(track["artists"]),
                "album": track.get("album")}

    @app.get("/playlists")
    def playlists() -> list[dict[str, Any]]:
        return store.list()

    @app.get("/playlists/summary")
    def playlist_summaries() -> list[dict[str, Any]]:
        return [{"id": playlist["id"], "name": playlist["name"], "trackCount": len(playlist["tracks"])}
                for playlist in store.list()]

    @app.post("/playlists")
    def create_playlist(body: dict[str, Any]) -> dict[str, Any]:
        try:
            document = normalize_playlist({"format": "echo-portable-library", "version": 1,
                                           "name": body.get("name"), "tracks": []})
            return store.save(document)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.get("/playlists/{playlist_id}")
    def get_playlist(playlist_id: str) -> dict[str, Any]:
        return store.get(playlist_id) or _not_found()

    @app.delete("/playlists/{playlist_id}")
    def delete_playlist(playlist_id: str) -> dict[str, bool]:
        if store.active_id() == playlist_id:
            raise HTTPException(409, '请先启用另一份歌单，再删除当前启用歌单')
        if not store.delete(playlist_id):
            _not_found()
        return {"deleted": True}

    @app.post("/playlists/{playlist_id}/tracks")
    def add_track(playlist_id: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            result = store.add_track(playlist_id, body)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        return result or _not_found()

    @app.post("/playlists/{playlist_id}/tracks/batch")
    def add_tracks(playlist_id: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            result = store.add_tracks(playlist_id, body.get("tracks"), body.get("offset")) or _not_found()
            return {"id": result["id"], "added": len(body["tracks"]), "trackCount": len(result["tracks"])}
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.post("/playlists/{playlist_id}/tracks/page")
    def playlist_tracks_page(playlist_id: str, body: dict[str, Any]) -> dict[str, Any]:
        playlist = store.get(playlist_id) or _not_found()
        try:
            page = int(body.get("page", 1))
            size = int(body.get("pageSize", 50))
        except (TypeError, ValueError) as error:
            raise HTTPException(400, "invalid pagination") from error
        if page < 1 or not 1 <= size <= 100:
            raise HTTPException(400, "invalid pagination")
        start = (page - 1) * size
        tracks = playlist["tracks"]
        return {"tracks": [{"index": index, "track": track} for index, track in
                           enumerate(tracks[start:start + size], start)],
                "total": len(tracks), "hasMore": start + size < len(tracks)}

    @app.get("/playlists/{playlist_id}/metadata")
    def playlist_metadata(playlist_id: str) -> dict[str, Any]:
        playlist = store.get(playlist_id) or _not_found()
        return {"format": playlist["format"], "version": playlist["version"], "name": playlist["name"]}

    @app.post("/playlists/{playlist_id}/rename")
    def rename_playlist(playlist_id: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return store.rename(playlist_id, body.get("name")) or _not_found()
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.delete("/playlists/{playlist_id}/tracks/{index}")
    def remove_track(playlist_id: str, index: int) -> dict[str, Any]:
        try:
            return store.remove_track(playlist_id, index) or _not_found()
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.post("/playlists/{playlist_id}/tracks/{index}/edit")
    def edit_track(playlist_id: str, index: int, body: dict[str, Any]) -> dict[str, Any]:
        try:
            result = store.edit_track(playlist_id, index, body) or _not_found()
            return {"id": result["id"], "track": result["tracks"][index]}
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.post("/playlists/{playlist_id}/albums/merge")
    def merge_album(playlist_id: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return store.merge_album(playlist_id, body.get("fromAlbum"), body.get("toAlbum"), body.get("albumArtist")) or _not_found()
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.post("/playlists/{playlist_id}/move")
    def move_track(playlist_id: str, body: dict[str, Any]) -> dict[str, Any]:
        source, target = body.get("from"), body.get("to")
        if (not isinstance(source, int) or isinstance(source, bool)
                or not isinstance(target, int) or isinstance(target, bool)):
            raise HTTPException(400, "from and to must be integer indexes")
        try:
            return store.move_track(playlist_id, source, target) or _not_found()
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.get("/playlists/{playlist_id}/export")
    def export_playlist(playlist_id: str) -> dict[str, Any]:
        playlist = store.get(playlist_id) or _not_found()
        return {key: value for key, value in playlist.items() if key != "id"}

    @app.post("/playlists/import")
    def import_playlist(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return store.save(normalize_playlist(body))
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.on_event('shutdown')
    def stop_workers():
        preparation['stop'] = True
        preparation_pool.shutdown(wait=False, cancel_futures=True)
        resolver_pool.shutdown(wait=False, cancel_futures=True)
        musicdl.close()

    return app


def _not_found() -> Any:
    raise HTTPException(404, "playlist not found")


app = make_app()


def run() -> None:
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=18765)
