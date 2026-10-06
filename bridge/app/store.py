"""SQLite storage for portable playlists, separate from ECHO's private database."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .core import clean_text, normalize_playlist, normalize_track


class PlaylistStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS playlists (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS playlist_ids (id TEXT PRIMARY KEY, ids TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS original_tracks (song_id TEXT PRIMARY KEY, document TEXT NOT NULL)")
        self.on_change = None

    def song_ids(self, playlist):
        with self.connect() as db:
            row = db.execute('SELECT ids FROM playlist_ids WHERE id=?', (playlist['id'],)).fetchone()
            ids = json.loads(row[0]) if row else [self.new_song_id(playlist['id'], i, t) for i, t in enumerate(playlist['tracks'])]
            if not row:
                db.execute('INSERT INTO playlist_ids VALUES (?,?)', (playlist['id'], json.dumps(ids)))
            db.executemany('INSERT OR IGNORE INTO original_tracks VALUES (?,?)', [(sid,json.dumps(t,ensure_ascii=False)) for sid,t in zip(ids,playlist['tracks'])])
            return ids

    def originals(self):
        with self.connect() as db:
            return {sid:json.loads(raw) for sid,raw in db.execute('SELECT song_id,document FROM original_tracks')}

    @staticmethod
    def new_song_id(playlist_id, index, track):
        fingerprint = json.dumps(track, ensure_ascii=False, sort_keys=True)
        return 'so_' + playlist_id + '_' + str(index) + '_' + hashlib.sha256(fingerprint.encode()).hexdigest()[:12]

    def active_id(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='active_playlist'").fetchone()
        if row:
            return row[0] or None
        playlists = [p for p in self.list() if p["tracks"]]
        chosen = max(playlists, key=lambda p: len(p['tracks']))['id'] if playlists else None
        if chosen:
            self.activate(chosen)
        return chosen

    def activate(self, playlist_id):
        playlist = self.get(playlist_id)
        if not playlist:
            raise ValueError('playlist not found')
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('active_playlist', ?)", (playlist_id,))

    def mounted(self):
        active = self.active_id()
        return [p for p in self.list() if p['id'] == active]

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def list(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT id, document FROM playlists ORDER BY rowid DESC").fetchall()
        return [{"id": row[0], **json.loads(row[1])} for row in rows]

    def get(self, playlist_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT document FROM playlists WHERE id=?", (playlist_id,)).fetchone()
        return {"id": playlist_id, **json.loads(row[0])} if row else None

    def save(self, document: dict[str, Any], playlist_id: str | None = None, identities=None) -> dict[str, Any]:
        normalized = normalize_playlist(document)
        previous = self.get(playlist_id) if playlist_id else None
        previous_ids = self.song_ids(previous) if previous else []
        playlist_id = playlist_id or uuid.uuid4().hex
        if identities is None:
            identities = previous_ids[:len(normalized['tracks'])]
            identities += [self.new_song_id(playlist_id, i, t) for i, t in enumerate(normalized['tracks']) if i >= len(identities)]
        if len(identities) != len(normalized['tracks']):
            raise ValueError('track identity count mismatch')
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO playlists (id, document) VALUES (?, ?)",
                       (playlist_id, json.dumps(normalized, ensure_ascii=False)))
            db.execute('INSERT OR REPLACE INTO playlist_ids VALUES (?,?)', (playlist_id, json.dumps(identities)))
            db.executemany('INSERT OR IGNORE INTO original_tracks VALUES (?,?)', [(sid,json.dumps(t,ensure_ascii=False)) for sid,t in zip(identities,normalized['tracks'])])
        if self.on_change and previous:
            old = dict(zip(previous_ids, previous['tracks']))
            for identity, track in zip(identities, normalized['tracks']):
                if identity in old and old[identity] != track:
                    self.on_change(identity)
        return {"id": playlist_id, **normalized}

    def delete(self, playlist_id: str) -> bool:
        with self.connect() as db:
            db.execute('DELETE FROM playlist_ids WHERE id=?', (playlist_id,))
            return db.execute("DELETE FROM playlists WHERE id=?", (playlist_id,)).rowcount > 0

    def add_track(self, playlist_id: str, track: dict[str, Any]) -> dict[str, Any] | None:
        document = self.get(playlist_id)
        if document is None:
            return None
        document["tracks"].append(normalize_track(track))
        return self.save(document, playlist_id)

    def add_tracks(self, playlist_id: str, tracks: list[dict[str, Any]], offset: int | None = None) -> dict[str, Any] | None:
        if not isinstance(tracks, list) or not 1 <= len(tracks) <= 250:
            raise ValueError("tracks must be an array of 1 to 250 items")
        normalized = [normalize_track(track) for track in tracks]
        document = self.get(playlist_id)
        if document is None:
            return None
        if offset is not None:
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                raise ValueError("offset must be a nonnegative integer")
            current = len(document["tracks"])
            if current >= offset + len(normalized) and document["tracks"][offset:offset + len(normalized)] == normalized:
                return document
            if current != offset:
                raise ValueError("playlist position mismatch")
        document["tracks"].extend(normalized)
        return self.save(document, playlist_id)

    def rename(self, playlist_id: str, name: str) -> dict[str, Any] | None:
        document = self.get(playlist_id)
        if document is None:
            return None
        document["name"] = clean_text(name, "name")
        return self.save(document, playlist_id)

    def remove_track(self, playlist_id: str, index: int) -> dict[str, Any] | None:
        document = self.get(playlist_id)
        if document is None:
            return None
        if not 0 <= index < len(document["tracks"]):
            raise ValueError("track index out of range")
        identities = self.song_ids(document)
        identities.pop(index)
        document["tracks"].pop(index)
        return self.save(document, playlist_id, identities)

    def edit_track(self, playlist_id: str, index: int, changes: dict[str, Any]) -> dict[str, Any] | None:
        document = self.get(playlist_id)
        if document is None:
            return None
        if not 0 <= index < len(document["tracks"]):
            raise ValueError("track index out of range")
        if not isinstance(changes, dict) or not set(changes) <= {"title", "artists", "album", "durationMs", "trackNumber", "discNumber", "sourceHints"}:
            raise ValueError("invalid track changes")
        document["tracks"][index] = normalize_track({**document["tracks"][index], **changes})
        return self.save(document, playlist_id)

    def merge_album(self, playlist_id: str, from_album: str, to_album: str, album_artist: str | None) -> dict[str, Any] | None:
        document = self.get(playlist_id)
        if document is None:
            return None
        old_name = clean_text(from_album, "fromAlbum")
        new_name = clean_text(to_album, "toAlbum")
        new_artist = clean_text(album_artist, "albumArtist") if album_artist is not None else None
        changed = 0
        for index, track in enumerate(document["tracks"]):
            if track.get("album") == old_name:
                document["tracks"][index] = normalize_track({**track, "album": new_name, "albumArtist": new_artist})
                changed += 1
        if changed:
            self.save(document, playlist_id)
        return {"id": playlist_id, "changed": changed}

    def move_track(self, playlist_id: str, source: int, target: int) -> dict[str, Any] | None:
        document = self.get(playlist_id)
        if document is None:
            return None
        tracks = document["tracks"]
        if not 0 <= source < len(tracks) or not 0 <= target < len(tracks):
            raise ValueError("track index out of range")
        identities = self.song_ids(document)
        identities.insert(target, identities.pop(source))
        tracks.insert(target, tracks.pop(source))
        return self.save(document, playlist_id, identities)

    def bulk(self, playlist_id, identities, action, target_id=None):
        """All playlist document mutations commit together; selection uses persistent IDs."""
        if action not in ('delete', 'copy', 'move') or not isinstance(identities, list) or not identities:
            raise ValueError('请选择歌曲并指定操作')
        source = self.get(playlist_id)
        if not source: raise ValueError('源歌单不存在')
        self.song_ids(source)
        if action != 'delete':
            if target_id == playlist_id: raise ValueError('请选择另一个歌单')
            target = self.get(target_id)
            if not target: raise ValueError('目标歌单不存在')
            self.song_ids(target)
        copied = []
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            source = json.loads(db.execute('SELECT document FROM playlists WHERE id=?',(playlist_id,)).fetchone()[0])
            ids = json.loads(db.execute('SELECT ids FROM playlist_ids WHERE id=?',(playlist_id,)).fetchone()[0])
            chosen = set(identities)
            if not chosen.issubset(ids): raise ValueError('歌曲列表已变化，请刷新后重选')
            selected = [(sid,t) for sid,t in zip(ids,source['tracks']) if sid in chosen]
            if action != 'delete':
                target = json.loads(db.execute('SELECT document FROM playlists WHERE id=?',(target_id,)).fetchone()[0])
                target_ids = json.loads(db.execute('SELECT ids FROM playlist_ids WHERE id=?',(target_id,)).fetchone()[0])
                for sid,track in selected:
                    new_id = 'so_' + target_id + '_' + uuid.uuid4().hex
                    target['tracks'].append(track);target_ids.append(new_id)
                    copied.append((sid,new_id))
                    original = db.execute('SELECT document FROM original_tracks WHERE song_id=?',(sid,)).fetchone()
                    db.execute('INSERT INTO original_tracks VALUES (?,?)',(new_id,original[0] if original else json.dumps(track,ensure_ascii=False)))
                target=normalize_playlist(target)
                db.execute('UPDATE playlists SET document=? WHERE id=?',(json.dumps(target,ensure_ascii=False),target_id))
                db.execute('UPDATE playlist_ids SET ids=? WHERE id=?',(json.dumps(target_ids),target_id))
            if action in ('delete','move'):
                source['tracks']=[t for sid,t in zip(ids,source['tracks']) if sid not in chosen]
                ids=[sid for sid in ids if sid not in chosen]
                db.execute('UPDATE playlists SET document=? WHERE id=?',(json.dumps(source,ensure_ascii=False),playlist_id))
                db.execute('UPDATE playlist_ids SET ids=? WHERE id=?',(json.dumps(ids),playlist_id))
        return {'changed':len(selected), 'copied':copied}
