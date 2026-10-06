import sys
import io
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import comparable, match_score, normalize_playlist
from app.providers import DirectCatalog, MusicDLSearch
from app.source_index import SourceIndex
from app.store import PlaylistStore


class BridgeCoreTests(unittest.TestCase):
    def test_source_index_persists_metadata_without_expiring_url(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "index.sqlite"
            index = SourceIndex(path)
            index.set("song-1", {"url": "https://example.com/expiring.mp3", "provider": "KuwoMusicClient",
                                 "sourceId": "123", "durationMs": 200000, "format": "mp3"})
            stored = SourceIndex(path).get("song-1")
            self.assertEqual(stored["status"], "ready")
            self.assertEqual(stored["durationMs"], 200000)
            self.assertNotIn("url", stored)
            self.assertEqual(SourceIndex(path).recent_url("song-1"), "https://example.com/expiring.mp3")
            index.forget_url("song-1")
            self.assertIsNone(SourceIndex(path).recent_url("song-1"))

    def test_headless_search_does_not_render_to_gbk_console(self):
        song = types.SimpleNamespace(song_name="ミク・初音", singers="Artist", album="Album", duration_s=123,
                                     identifier="id", source="KuwoMusicClient")
        class Provider:
            def search(self, **kwargs):
                progress = kwargs["main_process_context"]
                task = progress.add_task("ミク・初音", total=1)
                progress.advance(task)
                return [song]
        client = types.SimpleNamespace(music_clients={"KuwoMusicClient": Provider()})
        adapter = MusicDLSearch(["KuwoMusicClient"])
        with io.TextIOWrapper(io.BytesIO(), encoding="gbk") as gbk:
            with patch.object(adapter, "_client", return_value=client), patch.object(sys, "stdout", gbk):
                result = adapter._search_candidates("ミク・初音")
        self.assertEqual(result[0][0]["title"], "ミク・初音")

    def test_known_kuwo_id_refresh_avoids_search_and_preserves_format(self):
        song = types.SimpleNamespace(download_url="https://example.com/audio?id=123", with_valid_download_url=True,
                                     identifier="123", source="KuwoMusicClient", ext="mp3", duration_s=123)
        seen = []
        class Provider:
            def _parsewithofficialapiv1(self, **kwargs):
                seen.append(kwargs["search_result"]["MUSICRID"])
                return song
        adapter = MusicDLSearch(["KuwoMusicClient"])
        with patch.object(adapter, "_client", return_value=types.SimpleNamespace(music_clients={"KuwoMusicClient": Provider()})):
            result = adapter.refresh({"provider": "KuwoMusicClient", "sourceId": "123", "title": "Song",
                                      "artist": "Artist", "album": "Album", "matchScore": 90})
        self.assertEqual(seen, ["MUSIC_123"])
        self.assertEqual(result["format"], "mp3")
        self.assertEqual(result["sourceId"], "123")

    def test_album_separators_do_not_reject_same_recording(self):
        self.assertEqual(comparable("RUST _ 雲雀 _ 光芒"), comparable("RUST 雲雀 光芒"))
        wanted = {"title": "雲雀", "artists": ["ASCA"], "album": "RUST _ 雲雀 _ 光芒"}
        candidate = {"title": "雲雀", "artists": ["ASCA"], "album": "RUST 雲雀 光芒"}
        self.assertEqual(match_score(wanted, candidate), 90)

    def test_portable_roundtrip_does_not_keep_audio_url(self):
        with tempfile.TemporaryDirectory() as folder:
            store = PlaylistStore(Path(folder) / "playlists.sqlite")
            created = store.save({"format": "echo-portable-library", "version": 1,
                                  "name": "Test", "tracks": []})
            store.add_track(created["id"], {"title": "Nude", "artists": ["Radiohead"],
                                             "album": "In Rainbows", "url": "https://example.com/audio"})
            exported = store.get(created["id"])
            self.assertNotIn("url", exported["tracks"][0])
            imported = store.save(exported)
            self.assertEqual(imported["tracks"], exported["tracks"])
            self.assertTrue(store.delete(created["id"]))
            self.assertTrue(store.delete(imported["id"]))

    def test_album_context_rejects_wrong_edition(self):
        with tempfile.TemporaryDirectory() as folder:
            catalog = DirectCatalog(Path(folder) / "catalog.json")
            catalog.path.write_text('[{"title":"Nude","artists":["Radiohead"],'
                                    '"album":"Live","url":"https://example.com/test.mp3"}]', encoding="utf-8")
            wanted = normalize_playlist({"format": "echo-portable-library", "version": 1,
                                         "name": "Test", "tracks": [{"title": "Nude", "artists": ["Radiohead"],
                                                                       "album": "In Rainbows"}]})["tracks"][0]
            self.assertEqual(match_score(wanted, catalog.entries()[0]["track"]), 70)
            self.assertIsNone(catalog.resolve(wanted))

    def test_playlist_editing_preserves_order_in_export(self):
        with tempfile.TemporaryDirectory() as folder:
            store = PlaylistStore(Path(folder) / "playlists.sqlite")
            playlist = store.save({"format": "echo-portable-library", "version": 1,
                                   "name": "Original", "tracks": []})
            for title in ("One", "Two", "Three"):
                store.add_track(playlist["id"], {"title": title, "artists": ["Artist"]})
            store.rename(playlist["id"], "Renamed")
            store.move_track(playlist["id"], 2, 0)
            store.remove_track(playlist["id"], 1)
            exported = store.get(playlist["id"])
            self.assertEqual(exported["name"], "Renamed")
            self.assertEqual([track["title"] for track in exported["tracks"]], ["Three", "Two"])
            with self.assertRaises(ValueError):
                store.move_track(playlist["id"], 2, 0)

    def test_batch_add_is_atomic(self):
        with tempfile.TemporaryDirectory() as folder:
            store = PlaylistStore(Path(folder) / "playlists.sqlite")
            playlist = store.save({"format": "echo-portable-library", "version": 1,
                                   "name": "Album", "tracks": []})
            with self.assertRaises(ValueError):
                store.add_tracks(playlist["id"], [
                    {"title": "Valid", "artists": ["Artist"]},
                    {"title": "Invalid", "artists": []},
                ])
            self.assertEqual(store.get(playlist["id"])["tracks"], [])
            added = store.add_tracks(playlist["id"], [
                {"title": "Second", "artists": ["Artist"], "trackNumber": 2},
                {"title": "First", "artists": ["Artist"], "trackNumber": 1},
            ], offset=0)
            self.assertEqual([track["title"] for track in added["tracks"]], ["Second", "First"])
            retried = store.add_tracks(playlist["id"], [
                {"title": "Second", "artists": ["Artist"], "trackNumber": 2},
                {"title": "First", "artists": ["Artist"], "trackNumber": 1},
            ], offset=0)
            self.assertEqual(len(retried["tracks"]), 2)
            with self.assertRaises(ValueError):
                store.add_tracks(playlist["id"], [{"title": "Wrong", "artists": ["Artist"]}], offset=0)

    def test_direct_catalog_can_search_album_title(self):
        with tempfile.TemporaryDirectory() as folder:
            catalog = DirectCatalog(Path(folder) / "catalog.json")
            catalog.path.write_text('[{"title":"Nude","artists":["Radiohead"],'
                                    '"album":"In Rainbows","url":"https://example.com/test.mp3"}]',
                                    encoding="utf-8")
            self.assertEqual(catalog.search("In Rainbows")[0]["title"], "Nude")

    def test_musicdl_researches_at_playback_and_requires_valid_url(self):
        song = types.SimpleNamespace(song_name="Nude", singers="Radiohead", album="In Rainbows",
                                     duration_s=255, identifier="source-track", source="AuthorizedClient",
                                     download_url="https://example.com/test.mp3", with_valid_download_url=True,
                                     lyric="[00:01.00]Test line", cover_url="https://example.com/cover.jpg")
        class FakeClient:
            def __init__(self, music_sources, init_music_clients_cfg=None):
                self.sources = music_sources
            def search(self, keyword):
                return {"AuthorizedClient": [song]}
        package = types.ModuleType("musicdl")
        package.musicdl = types.SimpleNamespace(MusicClient=FakeClient)
        wanted = {"title": "Nude", "artists": ["Radiohead"], "album": "In Rainbows", "durationMs": 255000}
        with patch.dict(sys.modules, {"musicdl": package}):
            resolved = MusicDLSearch(["AuthorizedClient"]).resolve(wanted)
            self.assertEqual(resolved["url"], song.download_url)
            self.assertEqual(resolved["lyric"], song.lyric)
            self.assertEqual(resolved["coverUrl"], song.cover_url)
            song.with_valid_download_url = False
            self.assertIsNone(MusicDLSearch(["AuthorizedClient"]).resolve(wanted))

    def test_musicdl_falls_back_to_title_query(self):
        song = types.SimpleNamespace(song_name="Nude", singers="Radiohead", album="In Rainbows",
                                     duration_s=255, identifier="id", source="AuthorizedClient",
                                     download_url="https://example.com/test.mp3", with_valid_download_url=True)
        class FakeClient:
            def __init__(self, music_sources, init_music_clients_cfg=None):
                pass
            def search(self, keyword):
                return {"AuthorizedClient": [song] if keyword == "Nude" else []}
        package = types.ModuleType("musicdl")
        package.musicdl = types.SimpleNamespace(MusicClient=FakeClient)
        with patch.dict(sys.modules, {"musicdl": package}):
            result = MusicDLSearch(["AuthorizedClient"]).resolve({"title": "Nude", "artists": ["Radiohead"],
                                                                     "album": "In Rainbows", "durationMs": 255000})
            self.assertEqual(result["matchScore"], 100)

    def test_traditional_album_and_artist_alias_match_only_with_exact_title_and_album(self):
        wanted = {"title": "荒原", "artists": ["聲無哀樂乐队THEWEAPONS"], "album": "聲無·哀樂"}
        matching = {"title": "荒原", "artists": ["聲無哀樂乐队THEWEAPONS"], "album": "声无·哀乐"}
        unrelated = {"title": "荒原", "artists": ["任素汐"], "album": "荒原"}
        self.assertGreaterEqual(match_score(wanted, matching), 85)
        self.assertLess(match_score(wanted, unrelated), 85)


if __name__ == "__main__":
    unittest.main()
