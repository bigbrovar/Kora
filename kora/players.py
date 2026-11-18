"""Playback targets.

Two kinds of player share one interface so the UI does not care which is
active:

* LMSPlayer   a Squeezebox-protocol player known to the server (hardware,
              Squeezelite, piCorePlayer, ...). All work is JSON-RPC.
* LocalPlayer audio decoded inside this app with GStreamer, streaming the
              files straight from the server. Keeps its own queue.

Every public method may block on the network and is meant to be called
from a worker thread. status() returns the same normalised dict for both.
"""

import os
import random
import shutil
import socket
import subprocess
import threading

from .lms import Item, LMSError, _int

LOCAL_ID = "local"
VERBS = {"load": "play", "add": "add", "insert": "insert"}
ID_KEYS = {"album": "album_id", "artist": "artist_id", "genre": "genre_id",
           "track": "track_id", "playlist": "playlist_id", "year": "year"}


def empty_status():
    return {"mode": "stop", "time": 0.0, "duration": 0.0, "volume": 0,
            "shuffle": 0, "repeat": 0, "index": 0, "count": 0, "stamp": None,
            "title": "", "artist": "", "album": "", "art": "", "track_id": "",
            "power": True, "connected": True, "sync": []}


class LMSPlayer:
    is_local = False

    def __init__(self, client, info):
        self.client = client
        self.id = info["id"]
        self.name = info["name"]
        self.model = info.get("model", "")
        self.connected = info.get("connected", True)
        self.power = info.get("power", True)

    def _do(self, *cmd):
        return self.client.request(self.id, *cmd)

    # ------------------------------------------------------------------ state

    def status(self):
        r = self._do("status", "-", 1, "tags:adlcKNx")
        loop = r.get("playlist_loop") or []
        cur = loop[0] if loop else {}
        remote = r.get("remoteMeta") or {}
        st = empty_status()
        title = cur.get("title") or remote.get("title") or r.get("current_title") or ""
        track_id = str(cur.get("id", ""))
        group = [x for x in [r.get("sync_master", "")] + str(r.get("sync_slaves", "")).split(",") if x]
        st.update({
            "mode": r.get("mode", "stop"),
            "time": float(r.get("time") or 0),
            "duration": float(r.get("duration") or cur.get("duration") or 0),
            "volume": abs(_int(r.get("mixer volume"))),
            "shuffle": _int(r.get("playlist shuffle")),
            "repeat": _int(r.get("playlist repeat")),
            "index": _int(r.get("playlist_cur_index")),
            "count": _int(r.get("playlist_tracks")),
            "stamp": r.get("playlist_timestamp"),
            "title": title,
            "artist": cur.get("artist") or remote.get("artist") or "",
            "album": cur.get("album") or remote.get("album") or "",
            "track_id": track_id,
            "art": self.client.current_cover_url(self.id, f"{track_id}-{title}"[:80]) if loop else "",
            "power": bool(_int(r.get("power"), 1)),
            "connected": bool(_int(r.get("player_connected"), 1)),
            "sync": [x for x in group if x != self.id],
        })
        return st

    def queue(self, limit=1000):
        r = self._do("status", 0, limit, "tags:adlcKN")
        out = []
        for t in r.get("playlist_loop") or []:
            item = self.client._track(t)
            item.extra["index"] = _int(t.get("playlist index"))
            out.append(item)
        return out

    # --------------------------------------------------------------- transport

    def play(self):
        self._do("play")

    def pause(self):
        self._do("pause", 1)

    def toggle(self):
        mode = self._do("mode", "?").get("_mode", "stop")
        if mode == "play":
            self._do("pause", 1)
        elif mode == "pause":
            self._do("pause", 0)
        else:
            self._do("play")

    def stop(self):
        self._do("stop")

    def next(self):
        self._do("playlist", "index", "+1")

    def prev(self):
        self._do("playlist", "index", "-1")

    def seek(self, seconds):
        self._do("time", round(float(seconds), 1))

    def set_volume(self, volume):
        self._do("mixer", "volume", max(0, min(100, int(volume))))

    def set_shuffle(self, mode):
        self._do("playlist", "shuffle", int(mode))

    def set_repeat(self, mode):
        self._do("playlist", "repeat", int(mode))

    def set_power(self, on):
        self._do("power", 1 if on else 0)

    # ------------------------------------------------------------------- queue

    def play_item(self, item, mode="load", index=None):
        if item.kind in ID_KEYS:
            args = ["playlistcontrol", f"cmd:{mode}", f"{ID_KEYS[item.kind]}:{item.id}"]
            if index is not None and mode == "load":
                args.append(f"play_index:{index}")
            self._do(*args)
        elif item.kind == "plugin":
            self._do(item.extra["cmd"], "playlist", VERBS[mode], f"item_id:{item.id}")
        else:
            raise LMSError("This item cannot be played")

    def queue_jump(self, index):
        self._do("playlist", "index", int(index))

    def queue_remove(self, index):
        self._do("playlist", "delete", int(index))

    def queue_clear(self):
        self._do("playlist", "clear")

    def random_mix(self, what="tracks"):
        self._do("randomplay", what)

    # ------------------------------------------------- Don't Stop The Music

    def dstm_options(self):
        """Providers offered by the server's Don't Stop The Music plugin for
        this player, as dicts: {"text", "selected", "cmd"}. The first entry
        is always "Disabled". Raises LMSError when the plugin is switched off."""
        r = self._do("dontstopthemusicsetting")
        out = []
        for entry in r.get("item_loop") or []:
            cmd = ((entry.get("actions") or {}).get("do") or {}).get("cmd")
            if cmd:
                out.append({"text": entry.get("text", ""), "cmd": cmd,
                            "selected": bool(_int(entry.get("radio")))})
        return out

    def dstm_set(self, cmd):
        self._do(*cmd)

    # The Material Skin web interface keeps "the provider to go back to" on
    # the server when it switches Don't Stop The Music off. Using the same
    # slot keeps Kora and the web interface in step. Both calls are harmless
    # no-ops on servers without Material Skin.

    def dstm_saved(self):
        try:
            value = self._do("material-skin-client", "get-dstm").get("provider")
        except LMSError:
            return ""
        return "" if value in (None, "", 0, "0") else str(value)

    def dstm_save(self):
        try:
            self._do("material-skin-client", "save-dstm")
        except LMSError:
            pass

    # -------------------------------------------------------------------- sync

    def sync_add(self, other_id):
        """Make another player join this player's group."""
        self._do("sync", other_id)

    def unsync(self):
        self._do("sync", "-")


class LocalPlayer:
    """Plays on this computer through GStreamer. Library tracks and plain
    http(s) streams work; items that need the server to resolve them (most
    app and service content) require a real LMS player instead."""

    is_local = True
    id = LOCAL_ID
    model = "Built-in output"
    connected = True
    power = True

    def __init__(self, client, volume=80, on_error=None):
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        self.Gst = Gst
        Gst.init(None)
        self.client = client
        self.name = "This computer"
        self.on_error = on_error
        self._lock = threading.RLock()
        self._queue = []
        self._index = 0
        self._mode = "stop"
        self._shuffle = 0
        self._repeat = 0
        self._stamp = 0
        self._volume = int(volume)
        self.bin = Gst.ElementFactory.make("playbin", "kora-local")
        if self.bin is None:
            raise LMSError("GStreamer playbin is missing (install gstreamer1.0-plugins-base)")
        fake = Gst.ElementFactory.make("fakesink", None)
        if fake is not None:
            self.bin.set_property("video-sink", fake)
        # Optional override, e.g. KORA_AUDIO_SINK="pipewiresink" or
        # "alsasink device=hw:1,0" for a specific DAC.
        sink_desc = os.environ.get("KORA_AUDIO_SINK")
        if sink_desc:
            try:
                self.bin.set_property("audio-sink", Gst.parse_bin_from_description(sink_desc, True))
            except Exception as exc:
                raise LMSError(f"Bad KORA_AUDIO_SINK: {exc}") from exc
        self.bin.set_property("volume", self._volume / 100.0)
        self.bin.connect("source-setup", self._source_setup)
        bus = self.bin.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", self._on_eos)
        bus.connect("message::error", self._on_error)

    # --------------------------------------------------------------- internals

    def _source_setup(self, _bin, source):
        if self.client.user and source.find_property("user-id") is not None:
            uri = self.bin.get_property("current-uri") or self.bin.get_property("uri") or ""
            if uri.startswith(self.client.base):
                source.set_property("user-id", self.client.user)
                source.set_property("user-pw", self.client.password)

    def _on_eos(self, _bus, _msg):
        with self._lock:
            if self._repeat == 1:
                self._load(self._index)
            elif self._index + 1 < len(self._queue):
                self._load(self._index + 1)
            elif self._repeat == 2 and self._queue:
                self._load(0)
            else:
                self._halt()

    def _on_error(self, _bus, msg):
        err, _debug = msg.parse_error()
        self._halt()
        if self.on_error:
            self.on_error(f"Playback failed: {err.message}")

    def _halt(self):
        self.bin.set_state(self.Gst.State.NULL)
        self._mode = "stop"

    def _load(self, index, play=True):
        with self._lock:
            if not 0 <= index < len(self._queue):
                self._halt()
                return
            self._index = index
            url = self._queue[index].extra["stream"]
            self.bin.set_state(self.Gst.State.NULL)
            self.bin.set_property("uri", url)
            self.bin.set_state(self.Gst.State.PLAYING if play else self.Gst.State.PAUSED)
            self._mode = "play" if play else "pause"

    def _resolve(self, item):
        """Turn any library item into a flat list of streamable tracks."""
        c = self.client
        if item.kind == "track":
            tracks = [item]
        elif item.kind == "album":
            tracks, _ = c.tracks(0, 2000, album_id=item.id)
        elif item.kind == "artist":
            tracks, _ = c.tracks(0, 2000, artist_id=item.id)
        elif item.kind == "genre":
            tracks, _ = c.tracks(0, 500, genre_id=item.id)
        elif item.kind == "year":
            tracks, _ = c.tracks(0, 500, year=item.id)
        elif item.kind == "playlist":
            tracks, _ = c.playlist_tracks(item.id, 0, 2000)
        elif item.kind == "plugin" and item.extra.get("url", "").startswith(("http://", "https://")):
            tracks = [Item("track", "", item.title, subtitle=item.subtitle, art=item.art,
                           playable=True, extra={"artist": item.subtitle, "album": "",
                                                 "duration": 0.0, "stream": item.extra["url"]})]
        else:
            raise LMSError("This item needs an LMS player. Pick one from the player menu.")
        out = []
        for t in tracks:
            if "stream" not in t.extra:
                remote = t.extra.get("url", "")
                if remote.startswith(("http://", "https://")):
                    t.extra["stream"] = remote
                elif t.id and not t.id.startswith("-"):
                    t.extra["stream"] = c.track_url(t.id)
                else:
                    continue
            out.append(t)
        if not out:
            raise LMSError("Nothing playable was found in this item")
        return out

    # ------------------------------------------------------------------ state

    def status(self):
        Gst = self.Gst
        st = empty_status()
        with self._lock:
            cur = self._queue[self._index] if 0 <= self._index < len(self._queue) else None
            pos = dur = 0.0
            if cur is not None and self._mode != "stop":
                ok, p = self.bin.query_position(Gst.Format.TIME)
                if ok:
                    pos = p / Gst.SECOND
                ok, d = self.bin.query_duration(Gst.Format.TIME)
                if ok and d > 0:
                    dur = d / Gst.SECOND
            if cur is not None and not dur:
                dur = float(cur.extra.get("duration") or 0)
            st.update({
                "mode": self._mode, "time": pos, "duration": dur,
                "volume": self._volume, "shuffle": self._shuffle, "repeat": self._repeat,
                "index": self._index, "count": len(self._queue), "stamp": self._stamp,
            })
            if cur is not None:
                big = cur.art.replace("cover_100x100_o", "cover_300x300_o")
                st.update({"title": cur.title, "artist": cur.extra.get("artist", ""),
                           "album": cur.extra.get("album", ""), "art": big,
                           "track_id": cur.id})
        return st

    def queue(self, limit=1000):
        with self._lock:
            return list(self._queue[:limit])

    # --------------------------------------------------------------- transport

    def play(self):
        with self._lock:
            if self._mode == "pause":
                self.bin.set_state(self.Gst.State.PLAYING)
                self._mode = "play"
            elif self._mode == "stop" and self._queue:
                self._load(self._index)

    def pause(self):
        with self._lock:
            if self._mode == "play":
                self.bin.set_state(self.Gst.State.PAUSED)
                self._mode = "pause"

    def toggle(self):
        if self._mode == "play":
            self.pause()
        else:
            self.play()

    def stop(self):
        with self._lock:
            self._halt()

    def next(self):
        with self._lock:
            if self._index + 1 < len(self._queue):
                self._load(self._index + 1)
            elif self._repeat == 2 and self._queue:
                self._load(0)

    def prev(self):
        with self._lock:
            ok, pos = self.bin.query_position(self.Gst.Format.TIME)
            if (ok and pos > 3 * self.Gst.SECOND) or self._index == 0:
                self.seek(0)
            else:
                self._load(self._index - 1)

    def seek(self, seconds):
        Gst = self.Gst
        self.bin.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                             int(max(0.0, float(seconds)) * Gst.SECOND))

    def set_volume(self, volume):
        self._volume = max(0, min(100, int(volume)))
        self.bin.set_property("volume", self._volume / 100.0)

    def set_shuffle(self, mode):
        with self._lock:
            self._shuffle = 1 if mode else 0
            if self._shuffle and len(self._queue) > self._index + 2:
                tail = self._queue[self._index + 1:]
                random.shuffle(tail)
                self._queue[self._index + 1:] = tail
                self._stamp += 1

    def set_repeat(self, mode):
        self._repeat = int(mode) % 3

    def set_power(self, on):
        if not on:
            self.stop()

    # ------------------------------------------------------------------- queue

    def play_item(self, item, mode="load", index=None):
        tracks = self._resolve(item)
        with self._lock:
            self._stamp += 1
            if mode == "load":
                if self._shuffle and index is None:
                    random.shuffle(tracks)
                self._queue = tracks
                self._load(min(index or 0, len(tracks) - 1))
            elif mode == "insert":
                at = min(self._index + 1, len(self._queue))
                self._queue[at:at] = tracks
            else:
                self._queue.extend(tracks)
            if mode != "load" and self._mode == "stop" and len(self._queue) == len(tracks):
                self._load(0)

    def queue_jump(self, index):
        self._load(int(index))

    def queue_remove(self, index):
        with self._lock:
            if not 0 <= index < len(self._queue):
                return
            del self._queue[index]
            self._stamp += 1
            if index < self._index:
                self._index -= 1
            elif index == self._index:
                if self._queue and self._mode != "stop":
                    self._load(min(index, len(self._queue) - 1))
                else:
                    self._index = min(self._index, max(0, len(self._queue) - 1))
                    self._halt()

    def queue_clear(self):
        with self._lock:
            self._halt()
            self._queue = []
            self._index = 0
            self._stamp += 1

    def random_mix(self, what="tracks"):
        raise LMSError("Random mix needs an LMS player. Pick one from the player menu.")

    def dstm_options(self):
        raise LMSError("Don't Stop The Music runs on the server, so it needs an LMS player.")

    def shutdown(self):
        self.bin.set_state(self.Gst.State.NULL)


class Squeezelite:
    """Optionally run Squeezelite alongside the app so this computer shows up
    as a full LMS player (sync, radio, apps, gapless)."""

    def __init__(self):
        self.proc = None

    @staticmethod
    def binary():
        return shutil.which("squeezelite")

    @staticmethod
    def default_name():
        return f"Kora on {socket.gethostname()}"

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, host, name=""):
        binary = self.binary()
        if not binary or self.running():
            return self.running()
        try:
            self.proc = subprocess.Popen(
                [binary, "-s", host, "-n", name or self.default_name()],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self.proc = None
        return self.running()

    def stop(self):
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
