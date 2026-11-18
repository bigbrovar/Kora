"""Tiny JSON config store in ~/.config/kora/config.json."""

import json
import os

DEFAULTS = {
    "host": "",
    "port": 9000,
    "user": "",
    "password": "",
    "player": "",            # id of the last selected player
    "squeezelite": False,    # start a managed Squeezelite with the app
    "squeezelite_name": "",  # empty = "Kora on <hostname>"
    "local_volume": 80,
    "dstm_provider": "",     # last Don't Stop The Music provider, for the repeat button
    "album_view": "grid",    # "grid" or "list" for album pages
    "width": 1180,
    "height": 760,
}


class Config:
    def __init__(self, path=None):
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        self.path = path or os.path.join(base, "kora", "config.json")
        self.data = dict(DEFAULTS)
        try:
            with open(self.path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (OSError, ValueError):
            pass

    def get(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def set(self, key, value, save=True):
        if self.data.get(key) == value:
            return
        self.data[key] = value
        if save:
            self.save()

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass
