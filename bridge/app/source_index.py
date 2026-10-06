"""Persistent match metadata with a separate ten-minute playback URL cache."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from .metadata import clean_cover, clean_lyric


class SourceIndex:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS matches (
                song_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                checked_at REAL NOT NULL,
                details TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS recent_urls (
                song_id TEXT PRIMARY KEY, url TEXT NOT NULL, expires_at REAL NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS enrichment (
                song_id TEXT PRIMARY KEY, checked_at REAL NOT NULL, details TEXT NOT NULL
            )""")

    def invalidate(self, song_id):
        with self._connect() as db:
            for table in ('matches', 'recent_urls', 'enrichment'):
                db.execute(f'DELETE FROM {table} WHERE song_id=?', (song_id,))

    def metadata(self, song_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT checked_at, details FROM enrichment WHERE song_id=?", (song_id,)).fetchone()
        return {**json.loads(row[1]), "metadataCheckedAt": row[0]} if row else {}

    def set_metadata(self, song_id: str, details: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO enrichment VALUES(?,?,?)",
                       (song_id, time.time(), json.dumps(details, ensure_ascii=False)))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, song_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT status, checked_at, details FROM matches WHERE song_id=?", (song_id,)).fetchone()
            tags = db.execute("SELECT details FROM enrichment WHERE song_id=?", (song_id,)).fetchone()
        result = {"status": row[0], "checkedAt": row[1], **json.loads(row[2])} if row else {}
        return self._merge_tags(result, json.loads(tags[0]) if tags else {}) or None

    @staticmethod
    def _merge_tags(result: dict, tags: dict) -> dict:
        cover = clean_cover(tags.get('coverUrl')) or clean_cover(result.get('coverUrl'))
        lyric = clean_lyric(result.get('lyric')) or clean_lyric(tags.get('lyric'))
        if cover:
            result['coverUrl'] = cover
        elif 'coverUrl' in result:
            result.pop('coverUrl')
        if lyric:
            result['lyric'] = lyric
        if not result.get('durationMs') and tags.get('durationMs'):
            result['durationMs'] = tags['durationMs']
        if not result.get('sourceId') and tags.get('sourceId') and tags.get('matchScore', 0) >= 85:
            for field in ('sourceId', 'provider', 'title', 'artist', 'album', 'matchScore'):
                if tags.get(field) is not None:
                    result[field] = tags[field]
        if not result.get('album') and tags.get('album') and tags.get('matchScore', 0) >= 85:
            result['album'] = tags['album']
        if tags.get('artists') and tags.get('matchScore', 0) >= 85:
            result['artists'] = tags['artists']
            result['matchScore'] = max(result.get('matchScore', 0), tags['matchScore'])
        return result

    def all(self) -> dict[str, dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT song_id, status, checked_at, details FROM matches").fetchall()
            tags = db.execute("SELECT song_id, details FROM enrichment").fetchall()
        result = {song_id: {"status": status, "checkedAt": checked_at, **json.loads(details)}
                  for song_id, status, checked_at, details in rows}
        for song_id, details in tags:
            self._merge_tags(result.setdefault(song_id, {'status': 'metadata', 'checkedAt': 0}), json.loads(details))
        return result

    def set(self, song_id: str, source: dict[str, Any] | None) -> None:
        details = {} if source is None else {
            key: source[key] for key in ("provider", "sourceId", "title", "artist", "album", "matchScore",
                                          "durationMs", "format", "fileSizeBytes", "sampleRate", "bitDepth",
                                          "coverUrl", "lyric") if source.get(key) is not None
        }
        with self._connect() as db:
            db.execute("DELETE FROM recent_urls WHERE expires_at<=?", (time.time(),))
            previous = db.execute("SELECT details FROM matches WHERE song_id=?", (song_id,)).fetchone()
            if previous:
                old = json.loads(previous[0])
                if source is None:
                    details = old
                elif old.get("provider") == details.get("provider") and old.get("sourceId") == details.get("sourceId"):
                    # URL refreshes can omit tags. Retain known tags for the same recording.
                    details = {**old, **{key: value for key, value in details.items() if value not in (None, '', 'NULL')}}
            db.execute("INSERT OR REPLACE INTO matches(song_id,status,checked_at,details) VALUES(?,?,?,?)",
                       (song_id, "ready" if source else "unavailable", time.time(), json.dumps(details, ensure_ascii=False)))
            if source and isinstance(source.get("url"), str):
                db.execute("INSERT OR REPLACE INTO recent_urls(song_id,url,expires_at) VALUES(?,?,?)",
                           (song_id, source["url"], time.time() + 600))
            else:
                db.execute("DELETE FROM recent_urls WHERE song_id=?", (song_id,))

    def recent_url(self, song_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT url FROM recent_urls WHERE song_id=? AND expires_at>?",
                             (song_id, time.time())).fetchone()
        return row[0] if row else None

    def forget_url(self, song_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM recent_urls WHERE song_id=?", (song_id,))

    def counts(self) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute("SELECT status, COUNT(*) FROM matches GROUP BY status").fetchall()
        return {status: count for status, count in rows}
