"""Provider boundary. A playable URL must come from an authorized adapter."""

from __future__ import annotations

import json
import re
import io
import html
import logging
import time
from threading import BoundedSemaphore
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .core import match_score, normalize_track, unknown_artist


class DirectCatalog:
    """User supplied metadata and direct URLs for audio they may stream."""

    def __init__(self, path: Path):
        self.path = path

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        content = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(content, list):
            raise ValueError("catalog must be an array")
        entries = []
        for item in content:
            if not isinstance(item, dict):
                continue
            track = normalize_track(item)
            url = item.get("url")
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                continue
            entries.append({"track": track, "url": url})
        return entries

    def search(self, query: str) -> list[dict[str, Any]]:
        term = query.casefold().strip()
        return [entry["track"] for entry in self.entries()
                if term in (entry["track"]["title"] + " " + " ".join(entry["track"]["artists"])
                            + " " + (entry["track"].get("album") or "")).casefold()]

    def resolve(self, wanted: dict[str, Any]) -> dict[str, Any] | None:
        ranked = sorted(((match_score(wanted, entry["track"]), entry) for entry in self.entries()),
                        key=lambda pair: pair[0], reverse=True)
        threshold = 85 if wanted.get("album") else 70
        if not ranked or ranked[0][0] < threshold:
            return None
        if unknown_artist(wanted['artists']) and len({tuple(e['track']['artists']) for score, e in ranked if score >= threshold}) > 1:
            return None
        score, entry = ranked[0]
        return {"url": entry["url"], "title": entry["track"]["title"],
                "artist": ", ".join(entry["track"]["artists"]), "album": entry["track"].get("album"),
                "provider": "direct-catalog", "matchScore": score}


class MusicDLSearch:
    """Opt-in adapter: only exposes direct URLs from configured MusicDL sources."""

    def __init__(self, sources: list[str] | None = None, work_dir: Path | None = None):
        self.sources = sources or []
        self.work_dir = work_dir
        self.resolution_timeout = 8.0
        # Slow SDK fallbacks never consume the fast catalog workers. Admission
        # control prevents rapid random clicks from creating an unbounded queue.
        self.fast_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='source-fast')
        self.slow_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='source-fallback')
        self.fast_slots = BoundedSemaphore(4)
        self.slow_slots = BoundedSemaphore(2)

    def close(self):
        self.fast_pool.shutdown(wait=False, cancel_futures=True)
        self.slow_pool.shutdown(wait=False, cancel_futures=True)

    def _submit(self, run, slow=False):
        slots, pool = (self.slow_slots, self.slow_pool) if slow else (self.fast_slots, self.fast_pool)
        if not slots.acquire(blocking=False):
            return None
        def bounded():
            try:
                return run()
            finally:
                slots.release()
        try:
            return pool.submit(bounded)
        except Exception:
            slots.release()
            raise

    def warmup(self) -> None:
        if self.sources:
            from musicdl import musicdl  # Load the SDK before accepting playback requests.

    def search(self, query: str) -> list[dict[str, Any]]:
        if not self.sources:
            return []
        return [track for track, _ in self._search_candidates(query)]

    def _client(self, sources: list[str]):
        from musicdl import musicdl

        return musicdl.MusicClient(music_sources=sources, init_music_clients_cfg={
            source: {"search_size_per_source": 5, "search_size_per_page": 5,
                     "max_retries": 1, "disable_print": True,
                     "work_dir": str(self.work_dir or Path.cwd() / "musicdl-work")}
            for source in sources
        })

    @staticmethod
    def _source(song: Any, track: dict[str, Any], score: int) -> dict[str, Any] | None:
        url = getattr(song, "download_url", None)
        if not isinstance(url, str) or not bool(getattr(song, "with_valid_download_url", False)):
            return None
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        cover = getattr(song, "cover_url", None)
        cover_url = None
        if isinstance(cover, str):
            parsed_cover = urlparse(cover)
            if parsed_cover.scheme in {"http", "https"} and parsed_cover.hostname and not parsed_cover.username and not parsed_cover.password:
                cover_url = cover
        lyric = getattr(song, "lyric", None)
        lyric = ("\n".join(line for line in lyric.splitlines() if not line.startswith("[kuwo:"))[:262144]
                 if isinstance(lyric, str) else None)
        extension = str(getattr(song, "ext", "") or "").lower().lstrip(".")
        if not extension:
            extension = parsed.path.rsplit(".", 1)[-1].lower() if "." in parsed.path else None
        return {"url": url, "title": track["title"], "artist": ", ".join(track["artists"]),
                "album": track.get("album"), "provider": getattr(song, "source", "musicdl"),
                "sourceId": str(getattr(song, "identifier", "") or ""),
                "matchScore": score, "coverUrl": cover_url, "lyric": lyric,
                "durationMs": int(float(getattr(song, "duration_s", 0) or 0) * 1000) or None,
                "format": extension, "fileSizeBytes": getattr(song, "file_size_bytes", None)}

    def refresh(self, indexed: dict[str, Any]) -> dict[str, Any] | None:
        """Refresh a known source ID without resolving unrelated search results.

        This small adapter targets the pinned MusicDL 2.14 provider interface.
        Other providers retain the normal search fallback.
        """
        provider, identity = indexed.get("provider"), indexed.get("sourceId")
        if provider not in {"KuwoMusicClient", "NeteaseMusicClient"} or provider not in self.sources or not identity:
            return None
        client = self._client([provider]).music_clients[provider]
        parser = getattr(client, "_parsewithofficialapiv1", None)
        if parser is None:
            return None
        if provider == "KuwoMusicClient":
            raw = {"MUSICRID": "MUSIC_" + str(identity), "SONGNAME": indexed.get("title"),
                   "ARTIST": indexed.get("artist"), "ALBUM": indexed.get("album"),
                   "DURATION": (indexed.get("durationMs") or 0) / 1000, "pic": indexed.get("coverUrl")}
        else:
            raw = {"id": identity, "name": indexed.get("title"), "ar": [{"name": indexed.get("artist")}],
                   "al": {"name": indexed.get("album"), "picUrl": indexed.get("coverUrl")},
                   "dt": indexed.get("durationMs") or 0}
        try:
            song = parser(search_result=raw, request_overrides={"timeout": (3, 4)})
            if str(getattr(song, "identifier", "")) != str(identity):
                return None
            track = {"title": indexed.get("title") or "", "artists": [indexed.get("artist") or ""],
                     "album": indexed.get("album")}
            return self._source(song, track, indexed.get("matchScore", 0))
        except Exception as exc:
            logging.getLogger(__name__).info("Known-source refresh failed (%s)", type(exc).__name__)
            return None

    def _quick_kuwo(self, wanted: dict[str, Any], query: str) -> tuple[bool, dict[str, Any] | None]:
        """Match cheap catalog metadata before asking for an audio URL.

        The generic downloader resolves every result's audio before returning its
        search list. An online player only needs the selected recording.
        """
        wrapper = self._client(["KuwoMusicClient"])
        if not hasattr(wrapper, "music_clients"):
            return False, None
        client = wrapper.music_clients["KuwoMusicClient"]
        if not hasattr(client, "_constructsearchurls") or not hasattr(client, "_parsewithofficialapiv1"):
            return False, None
        try:
            search_url = client._constructsearchurls(keyword=query)[0]
            response = client.get(search_url, timeout=(3, 4))
            response.raise_for_status()
            rows = response.json().get("abslist")
            if not isinstance(rows, list):
                return False, None
            ranked = []
            for raw in rows:
                if not raw.get("SONGNAME") or not raw.get("ARTIST"):
                    continue
                candidate = normalize_track({"title": html.unescape(raw["SONGNAME"]),
                    "artists": [html.unescape(raw["ARTIST"])], "album": html.unescape(raw.get("ALBUM") or "") or None,
                    "durationMs": int(float(raw.get("DURATION") or 0) * 1000) or None})
                ranked.append((match_score(wanted, candidate), candidate, raw))
            ranked.sort(key=lambda item: item[0], reverse=True)
            threshold = 85 if wanted.get("album") else 70
            if not ranked or ranked[0][0] < threshold:
                return True, None
            if unknown_artist(wanted['artists']) and len({tuple(t['artists']) for score, t, _ in ranked if score >= threshold}) > 1:
                return True, None
            score, track, raw = ranked[0]
            song = client._parsewithofficialapiv1(search_result=raw, request_overrides={"timeout": (3, 4)})
            result = self._source(song, track, score)
            return bool(result), result
        except Exception as exc:
            logging.getLogger(__name__).info("Catalog match shortcut unavailable (%s)", type(exc).__name__)
            return False, None

    def _quick_netease(self, wanted: dict, query: str) -> tuple[bool, dict | None]:
        """Resolve only metadata matches, avoiding audio probes for unrelated hits."""
        try:
            client = self._client(['NeteaseMusicClient']).music_clients['NeteaseMusicClient']
            search = client._constructsearchurls(keyword=query)[0]
            response = client.post(search['url'], data=search['data'], timeout=(3, 4))
            response.raise_for_status()
            rows = response.json().get('result', {}).get('songs', [])
            ranked = []
            for raw in rows:
                try:
                    track = normalize_track({'title': raw.get('name'),
                        'artists': [a['name'] for a in raw.get('ar', [])],
                        'album': raw.get('al', {}).get('name'), 'durationMs': raw.get('dt') or None})
                except (ValueError, TypeError, KeyError):
                    continue
                ranked.append((match_score(wanted, track), track, raw))
            ranked.sort(key=lambda row: row[0], reverse=True)
            threshold = 85 if wanted.get('album') else 70
            eligible = [row for row in ranked if row[0] >= threshold]
            if unknown_artist(wanted['artists']) and len({tuple(t['artists']) for _, t, _ in eligible}) > 1:
                return True, None
            for score, track, raw in eligible[:2]:
                song = client._parsewithofficialapiv1(search_result=raw, request_overrides={'timeout': (3, 4)})
                if result := self._source(song, track, score):
                    return True, result
            # A matching official candidate without audio may still be served by
            # another SDK route. Preserve that existing fallback.
            return not eligible, None
        except Exception as exc:
            logging.getLogger(__name__).info('Netease catalog shortcut unavailable (%s)', type(exc).__name__)
            return False, None

    def resolve(self, wanted: dict[str, Any]) -> dict[str, Any] | None:
        if not self.sources:
            return None
        threshold = 85 if wanted.get("album") else 70
        title = wanted["title"]
        decoration = re.search(r"[（(][^）)]{20,}[）)]", title)
        # Long TV/film credits often appear in imported titles but not source catalogs.
        search_title = title[:decoration.start()].strip() if decoration and wanted.get("album") else title
        match_wanted = {**wanted, "title": search_title}
        credits = (wanted.get('album') or '') if unknown_artist(wanted['artists']) else ' '.join(wanted['artists'])
        queries = list(dict.fromkeys([f"{search_title} {credits}".strip(), search_title]))
        deadline = time.monotonic() + self.resolution_timeout
        cancelled = __import__('threading').Event()
        fast = [p for p in ('KuwoMusicClient', 'NeteaseMusicClient') if p in self.sources]
        sdk_fallbacks = set()
        def attempt(provider, quick):
            for query in queries:
                if cancelled.is_set() or time.monotonic() >= deadline:
                    return None
                if quick:
                    handled, result = (self._quick_kuwo if provider == 'KuwoMusicClient' else self._quick_netease)(match_wanted, query)
                    if result:
                        return result
                    if not handled:
                        sdk_fallbacks.add(provider)
                    continue
                ranked = sorted(((match_score(match_wanted, track), track, song)
                                 for track, song in self._search_candidates(query, [provider])),
                                key=lambda item: item[0], reverse=True)
                if unknown_artist(wanted['artists']) and len({tuple(t['artists']) for score, t, _ in ranked if score >= threshold}) > 1:
                    continue
                for score, track, song in ranked:
                    if score < threshold:
                        break
                    if result := self._source(song, track, score):
                        return result
            return None
        pending = {f for p in fast if (f := self._submit(lambda p=p: attempt(p, True)))}
        fast_busy = len(pending) < len(fast)
        # Only two slow SDK requests can exist at once, with no waiting queue.
        # Fast providers race; a stalled provider cannot delay a good result.
        remaining = None
        def fill_fallback():
            nonlocal remaining
            if remaining is None:
                remaining = list(dict.fromkeys([*sorted(sdk_fallbacks), *[p for p in self.sources if p not in fast]]))
            while remaining and len(pending) < 2:
                provider = remaining.pop(0)
                future = self._submit(lambda p=provider: attempt(p, False), slow=True)
                if future:
                    pending.add(future)
                else:
                    remaining.insert(0, provider)
                    break
        slow_started = not pending
        if slow_started:
            fill_fallback()
        try:
            while pending and time.monotonic() < deadline:
                done, pending = wait(pending, timeout=min(.5, max(0, deadline-time.monotonic())), return_when=FIRST_COMPLETED)
                for future in done:
                    try:
                        if result := future.result():
                            return result
                    except Exception as exc:
                        logging.getLogger(__name__).info('Source attempt failed: %s', type(exc).__name__)
                if not pending and not slow_started:
                    slow_started = True
                if slow_started:
                    fill_fallback()
            if pending or remaining or fast_busy or (fast and not slow_started):
                raise TimeoutError('source resolution budget exhausted or workers busy')
            return None
        finally:
            cancelled.set()
            # Network calls already running finish inside their bounded workers;
            # their late results are discarded and cannot restart playback.

    def _search_candidates(self, query: str, sources: list[str] | None = None) -> list[tuple[dict[str, Any], Any]]:
        sources = sources or self.sources
        client = self._client(sources)
        # The CLI wrapper always renders Rich progress, even with disable_print.
        # A hidden Windows service must not depend on console encoding or state.
        if hasattr(client, "music_clients"):
            from rich.console import Console
            from rich.progress import Progress

            def run(source):
                try:
                    with Progress(disable=True, console=Console(file=io.StringIO(), force_terminal=False)) as progress:
                        return source, client.music_clients[source].search(
                            keyword=query, num_threadings=1, request_overrides={"timeout": (3, 5)},
                            main_process_context=progress)
                except Exception as exc:
                    logging.getLogger(__name__).warning('Search provider %s failed: %s', source, type(exc).__name__)
                    return source, []
            with ThreadPoolExecutor(max_workers=min(len(sources), 5)) as pool:
                results = dict(pool.map(run, sources))
        else:
            results = client.search(keyword=query)
        tracks = []
        for source, songs in results.items():
            for song in songs:
                title = getattr(song, "song_name", None)
                artist = getattr(song, "singers", None)
                if not title or not artist:
                    continue
                try:
                    artists = [part.strip() for part in re.split(r"[,、/]", str(artist)) if part.strip()]
                    identifier = getattr(song, "identifier", None)
                    track = normalize_track({
                        "title": str(title), "artists": artists or [str(artist)],
                        "album": getattr(song, "album", None),
                        "durationMs": int(float(getattr(song, "duration_s", 0) or 0) * 1000) or None,
                        "sourceHints": {source: str(identifier)} if identifier else {},
                    })
                    tracks.append((track, song))
                except ValueError:
                    continue
        return tracks
