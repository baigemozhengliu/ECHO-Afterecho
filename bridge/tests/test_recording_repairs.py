import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlencode
from starlette.requests import Request
from app.core import match_score
from app.metadata import fetch_metadata
from app.track_metadata import effective_track
from app.store import PlaylistStore
from app.subsonic import make_subsonic_router, song_identity


class RecordingRepairTests(unittest.TestCase):
    def test_numeric_artist_is_repaired_without_mutating_original(self):
        raw = {'title': '(イチロクヨン)&狐子-ember', 'artists': ['未知艺术家'], 'trackNumber': 164}
        before = song_identity('playlist', 923, raw)
        result = effective_track(raw)
        self.assertEqual(result['title'], 'ember')
        self.assertEqual(result['artists'], ['164 (イチロクヨン)', '狐子'])
        self.assertIsNone(result['trackNumber'])
        self.assertEqual(before, song_identity('playlist', 923, raw))
        self.assertEqual(effective_track({**raw, 'trackNumber': 12})['title'], raw['title'])

    def test_missing_artist_requires_exact_title_and_album(self):
        wanted = {'title': 'Song', 'artists': ['未知艺术家'], 'album': 'Album'}
        candidate = {**wanted, 'artists': ['Actual Artist']}
        self.assertGreaterEqual(match_score(wanted, candidate), 85)
        self.assertLess(match_score(wanted, {**candidate, 'album': 'Other'}), 85)
        self.assertLess(match_score(wanted, {**candidate, 'title': 'Song live'}), 85)

    def test_unknown_artist_query_and_ambiguity(self):
        music = Mock(sources=['NeteaseMusicClient'])
        client = Mock()
        music._client.return_value.music_clients = {'NeteaseMusicClient': client}
        client._constructsearchurls.return_value = [{'url': 'https://example.com', 'data': {}}]
        client.post.return_value.json.return_value = {'result': {'songs': [
            {'name': 'Song', 'id': 1, 'ar': [{'name': 'A'}], 'al': {'name': 'Album'}},
            {'name': 'Song', 'id': 2, 'ar': [{'name': 'B'}], 'al': {'name': 'Album'}}]}}
        result = fetch_metadata(music, {'title': 'Song', 'artists': ['未知艺术家'], 'album': 'Album'}, {})
        self.assertNotIn('artists', result)
        client._constructsearchurls.assert_called_once_with(keyword='Song Album')
        client._parsewithofficialapiv1.assert_not_called()

    def test_first_song_detail_enriches_duration_without_audio_search(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = PlaylistStore(root / 'playlists.sqlite')
            store.save({'format': 'echo-portable-library', 'version': 1, 'name': 'Test',
                        'tracks': [{'title': 'Song', 'artists': ['未知艺术家'], 'album': 'Album'}]})
            playlist = store.list()[0]
            identity = song_identity(playlist['id'], 0, playlist['tracks'][0])
            music = Mock(sources=[])
            router = make_subsonic_router(store, music, root)
            endpoint = next(route.endpoint for route in router.routes if route.path == '/rest/{method}')
            auth = json.loads((root / 'subsonic-auth.json').read_text())
            query = urlencode({'u': auth['username'], 'p': auth['password'], 'f': 'json', 'id': identity})
            request = Request({'type': 'http', 'method': 'GET', 'query_string': query.encode(), 'headers': []})
            with patch('app.subsonic.fetch_metadata', return_value={'durationMs': 221019, 'artists': ['Actual'], 'matchScore': 85}):
                response = asyncio.run(endpoint('getSong', request))
            song = json.loads(response.body)['subsonic-response']['song']
            self.assertEqual(song['id'], identity)
            self.assertEqual(song['duration'], 221)
            self.assertEqual(song['artist'], 'Actual')
            music.resolve.assert_not_called()
            music.refresh.assert_not_called()
