"""Thread helpers and the artwork cache."""

import traceback
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from gi.repository import Gdk, GLib

_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="kora")
_art_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="kora-art")


def _idle(fn, *args):
    def run():
        try:
            fn(*args)
        except Exception:  # never let a callback kill the idle source silently
            traceback.print_exc()
        return False
    GLib.idle_add(run)


def run_async(work, done=None, failed=None, pool=None):
    """Run work() on a thread; deliver the result or the exception on the
    GTK main loop."""
    def job():
        try:
            result = work()
        except Exception as exc:  # reported to the UI, not swallowed
            if failed is not None:
                _idle(failed, exc)
            else:
                traceback.print_exc()
            return
        if done is not None:
            _idle(done, result)
    (pool or _pool).submit(job)


def fmt_time(seconds):
    seconds = int(max(0, seconds or 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


class ArtCache:
    """In-memory LRU of decoded artwork, fetched off the main thread."""

    def __init__(self, limit=400):
        self.limit = limit
        self.client = None
        self._textures = OrderedDict()
        self._waiting = {}

    def set_client(self, client):
        self.client = client
        self._textures.clear()
        self._waiting.clear()

    def get(self, url):
        tex = self._textures.get(url)
        if tex is not None:
            self._textures.move_to_end(url)
        return tex

    def load(self, url, callback):
        """callback(texture_or_None) always runs on the main loop."""
        if not url or self.client is None:
            callback(None)
            return
        tex = self.get(url)
        if tex is not None:
            callback(tex)
            return
        if url in self._waiting:
            self._waiting[url].append(callback)
            return
        self._waiting[url] = [callback]
        client = self.client
        run_async(lambda: client.fetch(url),
                  lambda data: self._decoded(url, data),
                  lambda _exc: self._finish(url, None),
                  pool=_art_pool)

    def _decoded(self, url, data):
        try:
            tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
        except GLib.Error:
            tex = None
        if tex is not None:
            self._textures[url] = tex
            while len(self._textures) > self.limit:
                self._textures.popitem(last=False)
        self._finish(url, tex)

    def _finish(self, url, tex):
        for cb in self._waiting.pop(url, []):
            cb(tex)
