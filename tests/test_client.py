#!/usr/bin/env python3
"""Checks the LMS client and both player backends against the mock server.

    python3 tests/test_client.py
"""

import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
os.environ.setdefault("KORA_AUDIO_SINK", "fakesink sync=true")

import mock_lms  # noqa: E402
from kora.lms import LMSClient, LMSError, discover  # noqa: E402
from kora.players import LMSPlayer, LocalPlayer  # noqa: E402

PORT = 9731


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = mock_lms.serve(PORT)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.c = LMSClient("127.0.0.1", PORT)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_server_status(self):
        info = self.c.server_status()
        self.assertEqual(len(info["players"]), 3)
        self.assertEqual(info["albums"], len(mock_lms.albums))

    def test_unreachable(self):
        with self.assertRaises(LMSError):
            LMSClient("127.0.0.1", 1, timeout=2).server_status()

    def test_discovery(self):
        found = discover(timeout=1.0)
        self.assertTrue(any(s["port"] == PORT for s in found), found)

    def test_library_browse(self):
        artists, total = self.c.artists(0, 5)
        self.assertEqual((len(artists), total), (5, len(mock_lms.artists)))
        albums, total = self.c.albums(0, 500)
        self.assertEqual(len(albums), total)
        self.assertTrue(albums[0].art.endswith("cover_300x300_o"))
        one, _ = self.c.albums(0, 50, artist_id=artists[0].id)
        self.assertTrue(one and all(a.subtitle == artists[0].title for a in one))
        tracks, _ = self.c.tracks(0, 100, album_id=one[0].id)
        self.assertEqual([t.extra["tracknum"] for t in tracks], list(range(1, len(tracks) + 1)))
        self.assertTrue(self.c.genres(0, 50)[0] and self.c.years(0, 50)[0])

    def test_paging(self):
        first, total = self.c.albums(0, 10)
        second, _ = self.c.albums(10, 10)
        self.assertEqual(len(first), 10)
        self.assertFalse({a.id for a in first} & {a.id for a in second})
        self.assertGreater(total, 20)

    def test_search_and_playlists(self):
        hits = self.c.search("lagos")
        self.assertTrue(hits)
        self.assertTrue(all("lagos" in h.title.lower() for h in hits))
        lists, _ = self.c.playlists(0, 10)
        tracks, total = self.c.playlist_tracks(lists[0].id, 0, 100)
        self.assertEqual(len(tracks), total)

    def test_plugins(self):
        radios, _ = self.c.radios()
        stations, _ = self.c.plugin_items(radios[0].extra["cmd"], 0, 50)
        self.assertTrue(stations[0].playable and stations[0].extra["url"].startswith("http"))
        apps, _ = self.c.apps()
        menu, _ = self.c.plugin_items(apps[0].extra["cmd"], 0, 50)
        self.assertEqual(menu[0].extra["type"], "search")
        self.assertTrue(menu[0].browsable and not menu[1].browsable and not menu[1].playable)
        favs, _ = self.c.favorites(0, 50)
        self.assertEqual(len(favs), 3)

    def test_artwork_fetch(self):
        album = self.c.albums(0, 1)[0][0]
        self.assertTrue(self.c.fetch(album.art).startswith(b"\x89PNG"))

    def test_lms_player(self):
        info = self.c.server_status()["players"][0]
        p = LMSPlayer(self.c, info)
        p.queue_clear()
        album = self.c.albums(0, 1)[0][0]
        n = len(self.c.tracks(0, 100, album_id=album.id)[0])
        p.play_item(album, "load", index=2)
        st = p.status()
        self.assertEqual((st["mode"], st["index"], st["count"]), ("play", 2, n))
        self.assertTrue(st["title"] and st["art"])
        self.assertTrue(self.c.fetch(st["art"]).startswith(b"\x89PNG"))   # titles contain spaces
        p.next()
        self.assertEqual(p.status()["index"], 3)
        p.toggle()
        self.assertEqual(p.status()["mode"], "pause")
        p.toggle()
        self.assertEqual(p.status()["mode"], "play")
        p.seek(42)
        self.assertAlmostEqual(p.status()["time"], 42, delta=2)
        p.set_volume(31)
        p.set_shuffle(1)
        p.set_repeat(2)
        st = p.status()
        self.assertEqual((st["volume"], st["shuffle"], st["repeat"]), (31, 1, 2))
        track = self.c.tracks(0, 1)[0][0]
        p.play_item(track, "add")
        queue = p.queue()
        self.assertEqual(len(queue), n + 1)
        self.assertEqual(queue[-1].title, track.title)
        p.queue_remove(len(queue) - 1)
        self.assertEqual(p.status()["count"], n)
        p.queue_jump(0)
        self.assertEqual(p.status()["index"], 0)
        other = self.c.server_status()["players"][1]["id"]
        p.sync_add(other)
        self.assertEqual(p.status()["sync"], [other])
        LMSPlayer(self.c, {"id": other, "name": "x"}).unsync()
        self.assertEqual(p.status()["sync"], [])
        options = p.dstm_options()
        self.assertEqual(options[0]["text"], "Disabled")
        self.assertTrue(options[0]["selected"])
        p.dstm_set(options[2]["cmd"])
        self.assertEqual([o["selected"] for o in p.dstm_options()], [False, False, True, False])
        p.dstm_set(options[0]["cmd"])
        self.assertTrue(p.dstm_options()[0]["selected"])
        station = self.c.favorites(0, 5)[0][0]
        p.play_item(station, "load")
        self.assertEqual(p.status()["title"], station.title)

    def test_local_player(self):
        from gi.repository import GLib
        errors = []
        p = LocalPlayer(self.c, 60, on_error=errors.append)
        loop = GLib.MainLoop()
        ctx = loop.get_context()

        def spin(seconds):
            end = time.time() + seconds
            while time.time() < end:
                ctx.iteration(False)
                time.sleep(0.01)

        album = self.c.albums(0, 1)[0][0]
        n = len(self.c.tracks(0, 100, album_id=album.id)[0])
        p.play_item(album, "load", index=1)
        spin(1.5)
        st = p.status()
        self.assertEqual((st["mode"], st["index"], st["count"]), ("play", 1, n), errors)
        self.assertGreater(st["time"], 0.3, errors)
        p.pause()
        self.assertEqual(p.status()["mode"], "pause")
        p.play()
        p.seek(5.2)          # mock tones are 6 s long, so EOS follows shortly
        spin(2.5)
        self.assertEqual(p.status()["index"], 2, errors)
        p.play_item(self.c.tracks(0, 1)[0][0], "insert")
        self.assertEqual(p.status()["count"], n + 1)
        self.assertEqual(p.queue()[3].title, self.c.tracks(0, 1)[0][0].title)
        p.queue_remove(3)
        p.next()
        self.assertEqual(p.status()["index"], 3)
        p.set_volume(20)
        self.assertEqual(p.status()["volume"], 20)
        with self.assertRaises(LMSError):
            p.play_item(self.c.plugin_items("podcasts", 0, 5)[0][1], "load")
        p.queue_clear()
        self.assertEqual((p.status()["mode"], p.status()["count"]), ("stop", 0))
        p.shutdown()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
