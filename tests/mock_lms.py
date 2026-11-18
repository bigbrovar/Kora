#!/usr/bin/env python3
"""A small fake Lyrion Music Server for developing and testing Kora without
a real one. Speaks enough of the JSON-RPC API, serves generated artwork and
short WAV tones, and answers UDP discovery.

    python3 tests/mock_lms.py [port]     (default 9000)
"""

import io
import json
import math
import random
import socket
import struct
import sys
import threading
import time
import wave
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

rng = random.Random(7)
ARTISTS = ["Fela Kuti", "Miles Davis", "Nina Simone", "Burna Boy", "Radiohead", "Asa",
           "Kraftwerk", "Sade", "King Sunny Ade", "Massive Attack", "Tems", "Daft Punk"]
WORDS = ["Night", "River", "Golden", "Echo", "Lagos", "Blue", "Signal", "Harmattan", "Paper",
         "Velvet", "Orbit", "Sunday", "Glass", "Ember", "Quiet", "Motion", "Salt", "Neon"]
GENRES = ["Afrobeat", "Jazz", "Soul", "Electronic", "Rock", "Highlife"]

artists = [{"id": i + 1, "artist": n} for i, n in enumerate(ARTISTS)]
genres = [{"id": i + 1, "genre": g} for i, g in enumerate(GENRES)]
albums, tracks = [], []
for a in artists:
    for _ in range(rng.randint(2, 5)):
        album = {"id": len(albums) + 1, "album": " ".join(rng.sample(WORDS, 2)),
                 "artist": a["artist"], "artist_id": a["id"],
                 "year": rng.randint(1969, 2025), "genre_id": rng.randint(1, len(GENRES)),
                 "added": rng.random()}
        albums.append(album)
        for n in range(rng.randint(6, 11)):
            tracks.append({"id": len(tracks) + 1, "title": " ".join(rng.sample(WORDS, rng.randint(1, 3))),
                           "artist": a["artist"], "artist_id": a["id"], "album": album["album"],
                           "album_id": album["id"], "tracknum": n + 1, "year": album["year"],
                           "genre_id": album["genre_id"], "duration": float(rng.randint(140, 420))})
        album["artwork_track_id"] = next(t["id"] for t in tracks if t["album_id"] == album["id"])
for t in tracks:
    t["coverid"] = str(next(a["artwork_track_id"] for a in albums if a["id"] == t["album_id"]))
playlists = [{"id": 9001, "playlist": "Friday Evening", "tracks": rng.sample(tracks, 14)},
             {"id": 9002, "playlist": "Focus", "tracks": rng.sample(tracks, 22)}]
DSTM = [("PLUGIN_RANDOM_TITLES", "Random Mix: Songs"), ("PLUGIN_RANDOM_ALBUM", "Random Mix: Albums"),
        ("PLUGIN_LASTMIX_DSTM", "LastMix: similar artists")]
STATIONS = [("NTS 1", "https://stream-relay-geo.ntslive.net/stream"),
            ("FIP", "https://icecast.radiofrance.fr/fip-midfi.mp3"),
            ("SomaFM Groove Salad", "https://ice1.somafm.com/groovesalad-128-mp3")]


class Player:
    def __init__(self, pid, name, model):
        self.id, self.name, self.model = pid, name, model
        self.queue, self.index, self.mode = [], 0, "stop"
        self.volume, self.shuffle, self.repeat, self.power = 55, 0, 0, 1
        self.stamp, self.sync, self._pos, self._since = time.time(), set(), 0.0, time.time()

    def pos(self):
        if self.mode == "play" and self.queue:
            p = self._pos + time.time() - self._since
            dur = self.queue[self.index]["duration"]
            while self.queue and p >= dur:
                p -= dur
                if self.index + 1 < len(self.queue):
                    self.index += 1
                elif self.repeat:
                    self.index = 0
                else:
                    self.mode, p = "stop", 0.0
                    break
                dur = self.queue[self.index]["duration"]
            self._pos, self._since = p, time.time()
            return p
        return self._pos

    def seek(self, p):
        self._pos, self._since = p, time.time()

    def start(self, index=0):
        self.index, self.mode = max(0, min(index, len(self.queue) - 1)), "play"
        self.seek(0.0)


players = {p.id: p for p in (Player("aa:bb:cc:00:00:01", "Living Room", "Squeezebox Touch"),
                             Player("aa:bb:cc:00:00:02", "Kitchen", "piCorePlayer"),
                             Player("aa:bb:cc:00:00:03", "Study", "SqueezeLite"))}


def params_of(args):
    out = {}
    for a in args:
        if isinstance(a, str) and ":" in a:
            key, value = a.split(":", 1)
            out[key] = value
    return out


def page(items, args):
    start = int(args[0]) if args and str(args[0]).lstrip("-").isdigit() else 0
    count = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else len(items)
    return items[start:start + count]


def match(text, term):
    return term is None or term.lower() in text.lower()


def select_tracks(p):
    out = tracks
    for key in ("album_id", "artist_id", "genre_id", "track_id"):
        if key in p:
            field = "id" if key == "track_id" else key
            out = [t for t in out if str(t[field]) == p[key]]
    if "year" in p:
        out = [t for t in out if str(t["year"]) == p["year"]]
    if "playlist_id" in p:
        out = next((pl["tracks"] for pl in playlists if str(pl["id"]) == p["playlist_id"]), [])
    return sorted(out, key=lambda t: (t["album_id"], t["tracknum"])) if "playlist_id" not in p else out


def handle(pid, cmd):
    name, args = cmd[0], [str(c) for c in cmd[1:]]
    p = params_of(args)
    pl = players.get(pid)
    if name == "serverstatus":
        return {"version": "9.1.0-mock", "info total albums": len(albums),
                "info total artists": len(artists), "info total songs": len(tracks),
                "player count": len(players),
                "players_loop": [{"playerid": x.id, "name": x.name, "modelname": x.model,
                                  "connected": 1, "power": x.power} for x in players.values()]}
    if name == "artists":
        res = [a for a in artists if match(a["artist"], p.get("search"))]
        if "genre_id" in p:
            ids = {t["artist_id"] for t in tracks if str(t["genre_id"]) == p["genre_id"]}
            res = [a for a in res if a["id"] in ids]
        return {"count": len(res), "artists_loop": page(res, args)}
    if name == "albums":
        res = [a for a in albums if match(a["album"], p.get("search"))]
        for key in ("artist_id", "genre_id", "year"):
            if key in p:
                res = [a for a in res if str(a[key]) == p[key]]
        sort = p.get("sort", "album")
        if sort == "new":
            res = sorted(res, key=lambda a: a["added"])
        elif sort == "random":
            res = random.sample(res, len(res))
        elif sort == "yearalbum":
            res = sorted(res, key=lambda a: (a["year"], a["album"]))
        else:
            res = sorted(res, key=lambda a: a["album"])
        return {"count": len(res), "albums_loop": page(res, args)}
    if name == "titles":
        res = [t for t in select_tracks(p) if match(t["title"], p.get("search"))]
        if p.get("sort") == "title":
            res = sorted(res, key=lambda t: t["title"])
        return {"count": len(res), "titles_loop": page(res, args)}
    if name == "genres":
        return {"count": len(genres), "genres_loop": page(genres, args)}
    if name == "years":
        ys = [{"year": y} for y in sorted({a["year"] for a in albums})]
        return {"count": len(ys), "years_loop": page(ys, args)}
    if name == "playlists":
        if args and args[0] == "tracks":
            res = select_tracks(p)
            return {"count": len(res), "playlisttracks_loop": page(res, args[1:])}
        res = [{"id": x["id"], "playlist": x["playlist"]} for x in playlists]
        return {"count": len(res), "playlists_loop": page(res, args)}
    if name == "radios":
        return {"count": 1, "radioss_loop": [{"cmd": "local", "name": "Internet Radio",
                                              "type": "xmlbrowser", "icon": "music/1/cover_100x100_o"}]}
    if name == "apps":
        return {"count": 1, "appss_loop": [{"cmd": "podcasts", "name": "Podcasts",
                                            "type": "xmlbrowser", "icon": "music/30/cover_100x100_o"}]}
    if name in ("favorites", "local", "podcasts"):
        if args and args[0] == "items":
            if name == "podcasts" and "item_id" not in p and "search" not in p:
                loop = [{"id": "0", "name": "Search podcasts", "type": "search", "hasitems": 1},
                        {"id": "1", "name": "No subscriptions yet", "type": "text", "hasitems": 0}]
            else:
                loop = [{"id": str(i), "name": n, "type": "audio", "isaudio": 1, "hasitems": 0,
                         "url": u, "image": f"music/{i * 9 + 3}/cover_100x100_o"}
                        for i, (n, u) in enumerate(STATIONS)]
            return {"count": len(loop), "loop_loop": page(loop, args[1:])}
        if args and args[0] == "playlist" and pl:
            n, _u = STATIONS[int(p.get("item_id", 0)) % len(STATIONS)]
            entry = {"id": -1, "title": n, "artist": "Radio", "album": "", "duration": 0.0 or 3600.0,
                     "coverid": "5"}
            if args[1] == "play":
                pl.queue = [entry]
                pl.start(0)
            else:
                pl.queue.append(entry)
            pl.stamp = time.time()
        return {}
    if name == "rescan":
        return {}
    if pl is None:
        return {}
    if name == "status":
        pos = pl.pos()
        res = {"player_name": pl.name, "player_connected": 1, "power": pl.power, "mode": pl.mode,
               "time": pos, "mixer volume": pl.volume, "playlist shuffle": pl.shuffle,
               "playlist repeat": pl.repeat, "playlist_cur_index": pl.index,
               "playlist_timestamp": pl.stamp, "playlist_tracks": len(pl.queue)}
        if pl.sync:
            res["sync_master"] = pl.id
            res["sync_slaves"] = ",".join(sorted(pl.sync))
        if pl.queue:
            res["duration"] = pl.queue[pl.index]["duration"]
            if args and args[0] == "-":
                rows = [(pl.index, pl.queue[pl.index])]
            else:
                rows = list(enumerate(pl.queue))[int(args[0]):int(args[0]) + int(args[1])]
            res["playlist_loop"] = [dict(t, **{"playlist index": i}) for i, t in rows]
        return res
    if name == "mode":
        return {"_mode": pl.mode}
    if name == "dontstopthemusicsetting":
        current = getattr(pl, "dstm", "")
        loop = [{"text": "Disabled", "radio": 0 if current else 1, "actions": {"do": {
            "player": 0, "cmd": ["playerpref", "plugin.dontstopthemusic:provider", 0]}}}]
        for token, text in DSTM:
            loop.append({"text": text, "radio": 1 if token == current else 0, "actions": {"do": {
                "player": 0, "cmd": ["playerpref", "plugin.dontstopthemusic:provider", token]}}})
        return {"offset": 0, "count": len(loop), "item_loop": loop}
    if name == "material-skin-client":      # the two calls Material Skin adds for DSTM
        if args[0] == "save-dstm":
            pl.dstm_saved = getattr(pl, "dstm", "")
            return {}
        if args[0] == "get-dstm":
            return {"provider": getattr(pl, "dstm_saved", "") or 0}
        return {}
    if name == "playerpref":
        if args[0] == "plugin.dontstopthemusic:provider":
            pl.dstm = "" if args[1] == "0" else args[1]
        return {}
    if name == "play":
        if pl.queue:
            pl.mode, pl._since = "play", time.time()
    elif name == "pause":
        want_pause = args[0] == "1" if args else pl.mode == "play"
        pos = pl.pos()
        pl.mode = "pause" if want_pause else "play"
        pl.seek(pos)
    elif name == "stop":
        pl.mode = "stop"
        pl.seek(0.0)
    elif name == "time":
        pl.seek(float(args[0]))
    elif name == "mixer":
        pl.volume = int(float(args[1]))
    elif name == "power":
        pl.power = int(args[0])
    elif name == "sync":
        if args[0] == "-":
            for other in players.values():
                other.sync.discard(pl.id)
            pl.sync.clear()
        else:
            pl.sync.add(args[0])
    elif name == "randomplay":
        pl.queue = random.sample(tracks, 25)
        pl.stamp = time.time()
        pl.start(0)
    elif name == "playlistcontrol":
        chosen = select_tracks(p)
        mode = p.get("cmd", "load")
        if mode == "load":
            pl.queue = list(chosen)
            if pl.shuffle and "play_index" not in p:
                random.shuffle(pl.queue)
            pl.start(int(p.get("play_index", 0)))
        elif mode == "insert":
            pl.queue[pl.index + 1:pl.index + 1] = chosen
        else:
            pl.queue.extend(chosen)
            if pl.mode == "stop" and len(pl.queue) == len(chosen):
                pl.start(0)
        pl.stamp = time.time()
    elif name == "playlist":
        sub = args[0]
        if sub == "index" and pl.queue:
            target = pl.index + int(args[1]) if args[1][0] in "+-" else int(args[1])
            pl.start(target % len(pl.queue) if pl.repeat else target)
        elif sub == "shuffle":
            pl.shuffle = int(args[1])
        elif sub == "repeat":
            pl.repeat = int(args[1])
        elif sub == "clear":
            pl.queue, pl.index, pl.mode = [], 0, "stop"
            pl.stamp = time.time()
        elif sub == "delete":
            i = int(args[1])
            if 0 <= i < len(pl.queue):
                del pl.queue[i]
                if i < pl.index:
                    pl.index -= 1
                pl.index = max(0, min(pl.index, len(pl.queue) - 1))
                pl.stamp = time.time()
    return {}


def png(seed, size=96):
    r = random.Random(seed)
    a = [r.randint(40, 230) for _ in range(3)]
    b = [r.randint(20, 200) for _ in range(3)]
    cx, cy, rad = r.uniform(.2, .8) * size, r.uniform(.2, .8) * size, r.uniform(.2, .5) * size
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            mix = y / size
            inside = (x - cx) ** 2 + (y - cy) ** 2 < rad ** 2
            for ch in range(3):
                v = a[ch] * (1 - mix) + b[ch] * mix
                rows.append(int(min(255, v + 45)) if inside else int(v))

    def chunk(tag, data):
        raw = tag + data
        return struct.pack(">I", len(data)) + raw + struct.pack(">I", zlib.crc32(raw) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b""))


def tone(seed, seconds=6, rate=22050):
    freq = 220 + (seed % 12) * 30
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * freq * i / rate)))
                               for i in range(seconds * rate)))
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def _send(self, body, ctype):
        rng_header = self.headers.get("Range", "")
        if rng_header.startswith("bytes=") and ctype.startswith("audio"):
            first, _, last = rng_header[6:].partition("-")
            start = int(first or 0)
            end = int(last) if last else len(body) - 1
            total, body = len(body), body[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        else:
            self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        pid, cmd = req["params"]
        try:
            result = handle(pid, cmd)
        except Exception as exc:  # report like a server fault
            self._send(json.dumps({"id": req.get("id"), "error": str(exc)}).encode(), "application/json")
            return
        self._send(json.dumps({"id": req.get("id"), "result": result}).encode(), "application/json")

    def do_GET(self):
        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "music":
            ident = parts[1]
            if ident == "current":
                pid = self.path.split("player=")[-1].split("&")[0].replace("%3A", ":")
                pl = players.get(pid)
                ident = pl.queue[pl.index]["coverid"] if pl and pl.queue else "0"
            seed = int(ident) if ident.lstrip("-").isdigit() else 0
            if parts[2].startswith("cover"):
                self._send(png(seed), "image/png")
            else:
                self._send(tone(seed), "audio/wav")
            return
        self.send_error(404)


def discovery(port):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 3483))
    except OSError:
        return
    while True:
        data, addr = sock.recvfrom(1024)
        if data.startswith(b"e"):
            name, js = b"Mock LMS", str(port).encode()
            sock.sendto(b"ENAME" + bytes([len(name)]) + name + b"JSON" + bytes([len(js)]) + js, addr)


def serve(port=9000):
    threading.Thread(target=discovery, args=(port,), daemon=True).start()
    server = ThreadingHTTPServer(("", port), Handler)
    server.handle_error = lambda *_a: None   # clients dropping connections is normal
    server.daemon_threads = True
    return server


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
    print(f"Mock LMS on http://localhost:{port} ({len(albums)} albums, {len(tracks)} tracks)")
    serve(port).serve_forever()
