import unittest,tempfile
from pathlib import Path
from app.store import PlaylistStore
from app.track_metadata import effective_track
from app.source_index import SourceIndex

class BulkTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=PlaylistStore(Path(self.tmp.name)/'p.db')
  self.a=self.store.save({'format':'echo-portable-library','version':1,'name':'A','tracks':[{'title':t,'artists':['歌手'],'album':'专辑'} for t in ['甲','乙','丙']]})
  self.b=self.store.save({'format':'echo-portable-library','version':1,'name':'B','tracks':[]})
  self.store.activate(self.a['id']);self.ids=self.store.song_ids(self.a)
 def tearDown(self):self.tmp.cleanup()
 def test_move_identity_preserves_survivors_and_original(self):
  self.store.edit_track(self.a['id'],1,{'title':'乙修正'})
  self.store.bulk(self.a['id'],[self.ids[1]],'move',self.b['id'])
  a=self.store.get(self.a['id']);b=self.store.get(self.b['id'])
  self.assertEqual(self.store.song_ids(a),[self.ids[0],self.ids[2]])
  self.assertEqual(b['tracks'][0]['title'],'乙修正')
  self.assertEqual(self.store.originals()[self.store.song_ids(b)[0]]['title'],'乙')
 def test_invalid_selection_atomic(self):
  with self.assertRaises(ValueError):self.store.bulk(self.a['id'],['missing'],'move',self.b['id'])
  self.assertEqual(len(self.store.get(self.a['id'])['tracks']),3)
  self.assertEqual(self.store.get(self.b['id'])['tracks'],[])
 def test_copy_independent_ids_and_clear_active(self):
  self.store.bulk(self.a['id'],self.ids,'copy',self.b['id'])
  self.assertFalse(set(self.ids)&set(self.store.song_ids(self.store.get(self.b['id']))))
  self.store.bulk(self.a['id'],self.ids,'delete')
  restored=PlaylistStore(self.store.path)
  self.assertEqual(restored.active_id(),self.a['id'])
  self.assertEqual(restored.mounted()[0]['tracks'],[])
  self.assertEqual(restored.song_ids(restored.mounted()[0]),[])
  self.assertEqual(len(restored.get(self.b['id'])['tracks']),3)
 def test_move_all_keeps_empty_mount(self):
  self.store.bulk(self.a['id'],self.ids,'move',self.b['id'])
  self.assertEqual(self.store.mounted()[0]['tracks'],[])
  self.assertEqual(len(self.store.get(self.b['id'])['tracks']),3)
 def test_metadata_missing_album_only(self):
  raw={'title':'歌曲','artists':['未知艺术家'],'album':None}
  tags={'artists':['歌手'],'album':'可靠专辑','matchScore':90}
  self.assertEqual(effective_track(raw,tags)['album'],'可靠专辑')
  self.assertEqual(effective_track({**raw,'album':'原专辑'},tags)['album'],'原专辑')
  self.assertIsNone(effective_track(raw,{**tags,'matchScore':20})['album'])

if __name__=='__main__':unittest.main()
