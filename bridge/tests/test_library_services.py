import io
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException

from app.store import PlaylistStore
from app.subsonic import make_subsonic_router


class LibraryServicesTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.store = PlaylistStore(self.root / "playlists.sqlite")
        self.track = {"title": "ミク・初音", "artists": ["Artist"], "album": "Record"}
        self.store.save({"format": "echo-portable-library", "version": 1, "name": "Test", "tracks": [self.track]})
        self.musicdl = Mock()
        self.musicdl.resolve.return_value = {"url": "https://example.com/test.mp3", "provider": "KuwoMusicClient",
                                            "sourceId": "123", "format": "mp3", "durationMs": 123000}
        self.pool = patch("app.subsonic.ThreadPoolExecutor")
        self.executor = self.pool.start().return_value
        self.services = {}
        router = make_subsonic_router(self.store, self.musicdl, self.root, self.services)
        self.endpoints = {route.path: route.endpoint for route in router.routes}

    def tearDown(self):
        self.pool.stop()
        self.folder.cleanup()

    def test_cover_and_lyrics_lookup_never_starts_a_search(self):
        response = self.endpoints["/metadata/lookup"]({"track": self.track})
        self.assertIsNone(response["source"])
        self.musicdl.resolve.assert_not_called()
        self.musicdl.refresh.assert_not_called()

    def test_imported_resolution_is_shared_across_callers(self):
        response = io.BytesIO(b"ID3" + b"\x00" * 61)
        response.headers = Message()
        response.headers["Content-Type"] = "audio/mpeg"
        response.headers["Content-Range"] = "bytes 0-63/123456"
        with patch("app.subsonic.urlopen", return_value=response):
            first = self.services["resolve_imported"](self.track)
            second = self.services["resolve_imported"](self.track)
        self.assertTrue(first[0])
        self.assertEqual(first, second)
        self.musicdl.resolve.assert_called_once()

    def test_queue_prefetch_is_bounded_and_only_matches_imported_tracks(self):
        unknown = {"title": "Not imported", "artists": ["Other"]}
        response = self.endpoints["/playback/queue"]({"current": self.track, "tracks": [self.track, unknown]})
        self.assertEqual(response, {"queued": 1})
        self.executor.submit.assert_called_once()
        with self.assertRaises(HTTPException) as rejected:
            self.endpoints["/playback/queue"]({"tracks": [self.track] * 3})
        self.assertEqual(rejected.exception.status_code, 400)

    def test_transient_audio_validation_recovers_without_new_search(self):
        response = io.BytesIO(b'ID3' + b'\x00' * 61)
        response.headers = Message()
        response.headers['Content-Type'] = 'audio/mpeg'
        response.headers['Content-Range'] = 'bytes 0-63/123456'
        with patch('app.subsonic.urlopen', side_effect=[TimeoutError(), response]) as network:
            found, source = self.services['resolve_imported'](self.track)
        self.assertTrue(found)
        self.assertEqual(source['durationMs'], 123000)
        self.assertEqual(network.call_count, 2)
        self.musicdl.resolve.assert_called_once()


if __name__ == "__main__":
    unittest.main()
