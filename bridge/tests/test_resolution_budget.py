import time
import unittest
from threading import Event
from unittest.mock import patch
from app.providers import MusicDLSearch


class ResolutionBudgetTests(unittest.TestCase):
    wanted = {'title': 'Song', 'artists': ['Artist'], 'album': 'Album'}

    def test_slow_provider_does_not_delay_matching_fast_provider(self):
        adapter = MusicDLSearch(['KuwoMusicClient', 'NeteaseMusicClient'])
        release = Event()
        def slow(*args):
            release.wait(1)
            return True, None
        try:
            with patch.object(adapter, '_quick_kuwo', side_effect=slow), patch.object(adapter, '_quick_netease', return_value=(True, {'provider': 'Netease'})):
                started = time.monotonic()
                self.assertEqual(adapter.resolve(self.wanted)['provider'], 'Netease')
                self.assertLess(time.monotonic()-started, .5)
        finally:
            release.set()
            adapter.close()

    def test_deadline_and_full_workers_are_transient_not_missing_song(self):
        adapter = MusicDLSearch(['KuwoMusicClient'])
        adapter.resolution_timeout = .05
        release = Event()
        def slow(*args):
            release.wait(1)
            return True, {'late': True}
        try:
            with patch.object(adapter, '_quick_kuwo', side_effect=slow):
                with self.assertRaises(TimeoutError):
                    adapter.resolve(self.wanted)
            held = [adapter.fast_slots.acquire(blocking=False) for _ in range(4)]
            with self.assertRaises(TimeoutError):
                adapter.resolve(self.wanted)
            for acquired in held:
                if acquired:
                    adapter.fast_slots.release()
        finally:
            release.set()
            adapter.close()
