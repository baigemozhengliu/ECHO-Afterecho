import asyncio
import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from starlette.requests import Request
from app.metadata import structured_lines, clean_lyric, clean_cover, fetch_metadata
from app.source_index import SourceIndex
from app.store import PlaylistStore
from app.subsonic import make_subsonic_router, song_identity


class MetadataTests(unittest.TestCase):
    def test_preparation_persists_recording_identity_without_audio_or_lyrics(self):
        music = Mock(sources=['NeteaseMusicClient'])
        client = Mock()
        music._client.return_value.music_clients = {'NeteaseMusicClient': client}
        client._constructsearchurls.return_value = [{'url': 'https://example.com/search', 'data': {}}]
        client.post.return_value.json.return_value = {'result': {'songs': [
            {'name': 'Song', 'id': 123, 'ar': [{'name': 'Artist'}], 'al': {'name': 'Album'}, 'dt': 123000}]}}
        tags = fetch_metadata(music, {'title': 'Song', 'artists': ['Artist'], 'album': 'Album'}, {}, identity_only=True)
        self.assertEqual(tags['sourceId'], '123')
        self.assertEqual(tags['durationMs'], 123000)
        merged = SourceIndex._merge_tags({}, tags)
        self.assertEqual(merged['provider'], 'NeteaseMusicClient')
        self.assertEqual(merged['artist'], 'Artist')
        client.post.assert_called_once()
        client._parsewithofficialapiv1.assert_not_called()
        music.resolve.assert_not_called()

    def test_fractional_and_repeated_lyric_stamps(self):
        self.assertEqual(structured_lines('[00:01.234][02:03.456]words'), [
            {'start': 1234, 'value': 'words'}, {'start': 123456, 'value': 'words'}])
        self.assertIsNone(clean_lyric('NULL'))

    def test_portraits_are_not_album_covers_and_existing_lyrics_win(self):
        self.assertIsNone(clean_cover('https://img.kuwo.cn/star/starheads/180/test.jpg'))
        self.assertIsNone(clean_cover('https://img.kuwo.cn/wmvpic/test.jpg'))
        result = SourceIndex._merge_tags({'lyric': '[00:01.234]original with translation'},
                                        {'lyric': '[00:01.000]fallback'})
        self.assertEqual(result['lyric'], '[00:01.234]original with translation')

    def test_metadata_never_resolves_audio(self):
        music = Mock(sources=['KuwoMusicClient'])
        client = music._client.return_value.music_clients.__getitem__.return_value if hasattr(music._client.return_value.music_clients, '__getitem__') else Mock()
        music._client.return_value.music_clients = {'KuwoMusicClient': client}
        client.get.return_value.json.return_value = {'data': {
            'songinfo': {'pic': 'https://example.com/cover.jpg'},
            'lrclist': [{'time': '1.23', 'lineLyric': 'text'}]}}
        result = fetch_metadata(music, {'title': 'Song', 'artists': ['Artist']},
            {'provider': 'KuwoMusicClient', 'sourceId': '123'})
        self.assertEqual(result['lyric'], '[00:01.23]text')
        self.assertEqual(result['coverUrl'], 'https://example.com/cover.jpg')
        music.resolve.assert_not_called()
        client._parsewithofficialapiv1.assert_not_called()

    def test_restart_uses_persisted_tags_and_cover_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = PlaylistStore(root / 'playlists.sqlite')
            store.save({'format': 'echo-portable-library', 'version': 1, 'name': 'Test',
                        'tracks': [{'title': 'Song', 'artists': ['Artist'], 'album': 'Album'}]})
            playlist = store.list()[0]
            track = playlist['tracks'][0]
            identity = song_identity(playlist['id'], 0, track)
            index = SourceIndex(root / 'source-index.sqlite')
            index.set(identity, {'provider': 'KuwoMusicClient', 'sourceId': '123',
                'lyric': '[00:01.234]text', 'coverUrl': 'https://example.com/a.jpg', 'durationMs': 123456})
            index.set(identity, {'provider': 'KuwoMusicClient', 'sourceId': '123', 'lyric': '', 'durationMs': None})
            music = Mock(sources=[])
            router = make_subsonic_router(store, music, root)
            routes = {route.path: route.endpoint for route in router.routes}
            auth = json.loads((root / 'subsonic-auth.json').read_text())
            async def call(method, **params):
                query = urlencode({'u': auth['username'], 'p': auth['password'], 'f': 'json', **params})
                request = Request({'type': 'http', 'method': 'GET', 'query_string': query.encode(), 'headers': []})
                return await routes['/rest/{method}'](method, request)
            lyric = json.loads(asyncio.run(call('getLyricsBySongId', id=identity)).body)
            self.assertEqual(lyric['subsonic-response']['lyricsList']['structuredLyrics'][0]['line'][0]['start'], 1234)
            song = json.loads(asyncio.run(call('getSong', id=identity)).body)['subsonic-response']['song']
            self.assertEqual(song['duration'], 123)
            album = json.loads(asyncio.run(call('getAlbum', id=song['albumId'])).body)['subsonic-response']['album']
            self.assertEqual(album['song'][0]['id'], identity)
            self.assertEqual(album['song'][0]['artist'], 'Artist')
            response = io.BytesIO(b'\xff\xd8\xfftest')
            response.headers = Message(); response.headers['Content-Type'] = 'image/jpg'
            with patch('app.subsonic.urlopen', return_value=response) as network:
                self.assertEqual(asyncio.run(call('getCoverArt', id=song['albumId'])).status_code, 200)
                self.assertEqual(asyncio.run(call('getCoverArt', id=song['albumId'])).status_code, 200)
                network.assert_called_once()
            music.resolve.assert_not_called()
            music.refresh.assert_not_called()
