"""Lyrion Music Server (LMS) JSON-RPC client.

Pure standard library, no GTK imports, so it can be tested and reused on
its own. Every call blocks; the UI runs them on worker threads.
"""

import base64
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field


class LMSError(Exception):
    pass


@dataclass
class Item:
    """One browsable or playable thing, whatever its source."""
    kind: str                 # artist album track genre year playlist plugin
    id: str
    title: str
    subtitle: str = ""
    art: str = ""             # absolute artwork URL, or ""
    playable: bool = False
    browsable: bool = False
    extra: dict = field(default_factory=dict)


def _int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def discover(timeout=2.0):
    """Find LMS servers on the LAN with the Squeezebox UDP discovery packet."""
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)
        packet = b"eIPAD\x00NAME\x00JSON\x00VERS\x00"
        for target in ("255.255.255.255", "127.0.0.1"):
            try:
                sock.sendto(packet, (target, 3483))
            except OSError:
                pass
        while True:
            try:
                data, addr = sock.recvfrom(1024)
            except (socket.timeout, OSError):
                break
            if not data.startswith(b"E"):
                continue
            info, pos = {}, 1
            while pos + 5 <= len(data):
                tag = data[pos:pos + 4].decode("ascii", "replace")
                size = data[pos + 4]
                info[tag] = data[pos + 5:pos + 5 + size].decode("utf-8", "replace")
                pos += 5 + size
            found[addr[0]] = {
                "name": info.get("NAME") or addr[0],
                "host": addr[0],
                "port": _int(info.get("JSON"), 9000),
                "version": info.get("VERS", ""),
            }
    finally:
        sock.close()
    # A server on this machine answers on loopback and on its LAN address.
    lan = {(s["name"], s["port"]) for s in found.values() if not s["host"].startswith("127.")}
    return [s for s in found.values()
            if not (s["host"].startswith("127.") and (s["name"], s["port"]) in lan)]


class LMSClient:
    def __init__(self, host, port=9000, user="", password="", timeout=10):
        self.host = host.strip()
        self.port = int(port or 9000)
        self.user = user or ""
        self.password = password or ""
        self.timeout = timeout
        self.base = f"http://{self.host}:{self.port}"

    # ---------------------------------------------------------------- plumbing

    def _auth(self):
        if not self.user:
            return {}
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        return {"Authorization": "Basic " + token}

    def request(self, player, *command):
        """Send one slim.request and return its result dict."""
        payload = json.dumps({
            "id": 1,
            "method": "slim.request",
            "params": [player or "", [c if isinstance(c, (int, float)) else str(c) for c in command]],
        }).encode()
        headers = {"Content-Type": "application/json"}
        headers.update(self._auth())
        req = urllib.request.Request(self.base + "/jsonrpc.js", data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise LMSError("The server rejected the username or password") from exc
            raise LMSError(f"Server error {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            raise LMSError(f"Cannot reach {self.host}:{self.port} ({reason})") from exc
        if body.get("error"):
            raise LMSError(str(body["error"]))
        return body.get("result") or {}

    def fetch(self, url, timeout=15):
        """Download bytes (artwork). Credentials are only sent to the LMS host."""
        headers = self._auth() if url.startswith(self.base) else {}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def abs_url(self, url):
        if not url:
            return ""
        if url.startswith(("http://", "https://")):
            return url
        return self.base + "/" + url.lstrip("/")

    def cover_url(self, art_id, size=300):
        return f"{self.base}/music/{art_id}/cover_{size}x{size}_o" if art_id not in (None, "") else ""

    def current_cover_url(self, player_id, stamp="", size=300):
        # The extra k= value only makes the URL unique per track for caching.
        return (f"{self.base}/music/current/cover_{size}x{size}_o"
                f"?player={urllib.parse.quote(str(player_id))}&k={urllib.parse.quote(str(stamp), safe='')}")

    def track_url(self, track_id):
        return f"{self.base}/music/{track_id}/download"

    # ------------------------------------------------------------------ server

    def server_status(self):
        r = self.request("", "serverstatus", 0, 100)
        players = []
        for p in r.get("players_loop") or []:
            players.append({
                "id": p.get("playerid", ""),
                "name": p.get("name", "Player"),
                "model": p.get("modelname") or p.get("model", ""),
                "connected": bool(_int(p.get("connected"), 1)),
                "power": bool(_int(p.get("power"), 1)),
            })
        return {
            "version": r.get("version", ""),
            "albums": _int(r.get("info total albums")),
            "artists": _int(r.get("info total artists")),
            "songs": _int(r.get("info total songs")),
            "scanning": bool(_int(r.get("rescan"))),
            "players": players,
        }

    def rescan(self):
        self.request("", "rescan")

    # ----------------------------------------------------------------- library

    @staticmethod
    def _filters(**kw):
        return [f"{k}:{v}" for k, v in kw.items() if v not in (None, "")]

    def artists(self, start, count, genre_id=None, search=None):
        r = self.request("", "artists", start, count,
                         *self._filters(genre_id=genre_id, search=search))
        items = [Item("artist", str(a.get("id")), a.get("artist", "Unknown artist"),
                      playable=True, browsable=True)
                 for a in r.get("artists_loop") or []]
        return items, _int(r.get("count"))

    def albums(self, start, count, artist_id=None, genre_id=None, year=None,
               sort="album", search=None):
        r = self.request("", "albums", start, count, "tags:aljyS", f"sort:{sort}",
                         *self._filters(artist_id=artist_id, genre_id=genre_id,
                                        year=year, search=search))
        items = []
        for a in r.get("albums_loop") or []:
            year_val = _int(a.get("year"))
            items.append(Item(
                "album", str(a.get("id")), a.get("album", "Unknown album"),
                subtitle=a.get("artist", ""),
                art=self.cover_url(a.get("artwork_track_id")),
                playable=True, browsable=True,
                extra={"year": year_val or "", "artist_id": a.get("artist_id")}))
        return items, _int(r.get("count"))

    def _track(self, t):
        duration = float(t.get("duration") or 0)
        art = self.abs_url(t.get("artwork_url")) or self.cover_url(t.get("coverid"), 100)
        return Item(
            "track", str(t.get("id")), t.get("title", "Unknown title"),
            subtitle=" · ".join(x for x in (t.get("artist"), t.get("album")) if x),
            art=art, playable=True,
            extra={"artist": t.get("artist", ""), "album": t.get("album", ""),
                   "duration": duration, "tracknum": _int(t.get("tracknum")),
                   "url": t.get("url", "")})

    def tracks(self, start, count, album_id=None, artist_id=None, genre_id=None,
               year=None, search=None, sort=None):
        if sort is None:
            sort = "tracknum" if album_id else ("albumtrack" if artist_id else "title")
        r = self.request("", "titles", start, count, "tags:adlcty", f"sort:{sort}",
                         *self._filters(album_id=album_id, artist_id=artist_id,
                                        genre_id=genre_id, year=year, search=search))
        return [self._track(t) for t in r.get("titles_loop") or []], _int(r.get("count"))

    def genres(self, start, count):
        r = self.request("", "genres", start, count)
        items = [Item("genre", str(g.get("id")), g.get("genre", "Unknown"),
                      playable=True, browsable=True)
                 for g in r.get("genres_loop") or []]
        return items, _int(r.get("count"))

    def years(self, start, count):
        r = self.request("", "years", start, count)
        items = [Item("year", str(y.get("year")), str(y.get("year") or "Unknown"),
                      playable=True, browsable=True)
                 for y in r.get("years_loop") or []]
        items.sort(key=lambda i: i.id, reverse=True)
        return items, _int(r.get("count"))

    def playlists(self, start, count, search=None):
        r = self.request("", "playlists", start, count, *self._filters(search=search))
        items = [Item("playlist", str(p.get("id")), p.get("playlist", "Playlist"),
                      playable=True, browsable=True)
                 for p in r.get("playlists_loop") or []]
        return items, _int(r.get("count"))

    def playlist_tracks(self, playlist_id, start, count):
        r = self.request("", "playlists", "tracks", start, count,
                         f"playlist_id:{playlist_id}", "tags:adlcKtyu")
        return [self._track(t) for t in r.get("playlisttracks_loop") or []], _int(r.get("count"))

    def search(self, term, limit=30):
        """Artists, albums and songs matching a term, in that order."""
        out = []
        artists, _ = self.artists(0, 10, search=term)
        for a in artists:
            a.subtitle = "Artist"
        out += artists
        albums, _ = self.albums(0, 20, search=term)
        for a in albums:
            a.subtitle = " · ".join(x for x in ("Album", a.subtitle) if x)
        out += albums
        tracks, _ = self.tracks(0, limit, search=term)
        out += tracks
        return out

    # ------------------------------------------- radio, apps, favorites (XMLBrowser)

    def _plugin_roots(self, command, loop):
        r = self.request("", command, 0, 200)
        items = []
        for p in r.get(loop) or []:
            cmd = p.get("cmd")
            if not cmd:
                continue
            items.append(Item("plugin", "", p.get("name", cmd), art=self.abs_url(p.get("icon")),
                              browsable=True,
                              extra={"cmd": cmd, "type": p.get("type", ""), "root": True}))
        return items, len(items)

    def radios(self, start=0, count=200):
        return self._plugin_roots("radios", "radioss_loop")

    def apps(self, start=0, count=200):
        return self._plugin_roots("apps", "appss_loop")

    def plugin_items(self, cmd, start, count, item_id=None, search=None, player=""):
        """Browse any XMLBrowser-style menu: radio, apps, favorites."""
        r = self.request(player, cmd, "items", start, count, "want_url:1",
                         *self._filters(item_id=item_id, search=search))
        items = []
        for e in r.get("loop_loop") or []:
            kind = e.get("type", "")
            has_items = bool(_int(e.get("hasitems")))
            audio = bool(_int(e.get("isaudio"))) or kind in ("audio", "playlist")
            items.append(Item(
                "plugin", str(e.get("id", "")), e.get("name") or e.get("title") or "",
                subtitle=e.get("line2") or e.get("subtext") or "",
                art=self.abs_url(e.get("image") or e.get("icon")),
                playable=audio,
                browsable=has_items or kind == "search",
                extra={"cmd": cmd, "type": kind, "url": e.get("url", "")}))
        return items, _int(r.get("count"), len(items))

    def favorites(self, start, count, item_id=None):
        return self.plugin_items("favorites", start, count, item_id=item_id)
