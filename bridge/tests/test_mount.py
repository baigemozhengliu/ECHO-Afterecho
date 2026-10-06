import tempfile
import unittest
from pathlib import Path
from app.store import PlaylistStore
from app.subsonic import song_identity


class MountTests(unittest.TestCase):
    def test_ids_survive_edit_move_delete_and_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'playlists.sqlite'
            store = PlaylistStore(path)
            p = store.save({'format': 'echo-portable-library', 'version': 1, 'name': 'Library',
                            'tracks': [{'title': t, 'artists': ['Artist']} for t in ['One', 'Two', 'Three']]})
            ids = store.song_ids(p)
            self.assertEqual(ids, [song_identity(p['id'], i, t) for i,t in enumerate(p['tracks'])])
            store.edit_track(p['id'], 0, {'title': 'Corrected'})
            store.move_track(p['id'], 0, 2)
            store.remove_track(p['id'], 0)
            restored = PlaylistStore(path)
            self.assertEqual(restored.song_ids(restored.get(p['id'])), [ids[2], ids[0]])
            self.assertEqual(restored.originals()[ids[0]]['title'], 'One')

    def test_only_active_playlist_is_mounted_including_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            store = PlaylistStore(Path(temp) / 'playlists.sqlite')
            a = store.save({'format': 'echo-portable-library', 'version': 1, 'name': 'Default',
                            'tracks': [{'title': 'One', 'artists': ['Artist']}]})
            b = store.save({'format': 'echo-portable-library', 'version': 1, 'name': 'Draft', 'tracks': []})
            self.assertEqual(store.active_id(), a['id'])
            store.activate(b['id'])
            self.assertEqual([p['id'] for p in store.mounted()], [b['id']])
            self.assertEqual(store.mounted()[0]['tracks'], [])
            store.add_track(b['id'], {'title': 'Two', 'artists': ['Artist']})
            store.activate(b['id'])
            self.assertEqual([p['id'] for p in store.mounted()], [b['id']])
            self.assertEqual(len(store.list()), 2)
