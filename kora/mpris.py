"""Minimal MPRIS2 service so desktop media keys and shell widgets control
whichever player is active in Kora. Fails quietly when there is no session bus."""

from gi.repository import Gio, GLib

from . import APP_ID, APP_NAME

XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg name="Offset" type="x" direction="in"/></method>
    <method name="SetPosition">
      <arg name="TrackId" type="o" direction="in"/>
      <arg name="Position" type="x" direction="in"/>
    </method>
    <method name="OpenUri"><arg name="Uri" type="s" direction="in"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Rate" type="d" access="read"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""

PATH = "/org/mpris/MediaPlayer2"
ROOT = "org.mpris.MediaPlayer2"
PLAYER = "org.mpris.MediaPlayer2.Player"
STATUS = {"play": "Playing", "pause": "Paused", "stop": "Stopped"}


class Mpris:
    def __init__(self, window):
        self.win = window
        self.conn = None
        self.st = None
        self._last = None
        try:
            self._info = Gio.DBusNodeInfo.new_for_xml(XML)
            self._owner = Gio.bus_own_name(
                Gio.BusType.SESSION, "org.mpris.MediaPlayer2.kora",
                Gio.BusNameOwnerFlags.NONE, self._on_bus, None, None)
        except GLib.Error:
            self._owner = 0

    def _on_bus(self, conn, _name):
        self.conn = conn
        try:
            for iface in self._info.interfaces:
                conn.register_object(PATH, iface, self._call, self._get, self._set)
        except GLib.Error:
            self.conn = None

    # ----------------------------------------------------------------- export

    def _props(self, iface):
        st = self.st or {}
        if iface == ROOT:
            return {
                "CanQuit": GLib.Variant("b", True), "CanRaise": GLib.Variant("b", True),
                "HasTrackList": GLib.Variant("b", False),
                "Identity": GLib.Variant("s", APP_NAME),
                "DesktopEntry": GLib.Variant("s", APP_ID),
                "SupportedUriSchemes": GLib.Variant("as", []),
                "SupportedMimeTypes": GLib.Variant("as", []),
            }
        meta = {"mpris:trackid": GLib.Variant("o", "/app/kora/track/current")}
        if st.get("title"):
            meta["xesam:title"] = GLib.Variant("s", st["title"])
            meta["xesam:artist"] = GLib.Variant("as", [st.get("artist") or ""])
            meta["xesam:album"] = GLib.Variant("s", st.get("album") or "")
            meta["mpris:length"] = GLib.Variant("x", int(st.get("duration", 0) * 1e6))
            if st.get("art"):
                meta["mpris:artUrl"] = GLib.Variant("s", st["art"])
        has = bool(st.get("count"))
        return {
            "PlaybackStatus": GLib.Variant("s", STATUS.get(st.get("mode"), "Stopped")),
            "Rate": GLib.Variant("d", 1.0), "MinimumRate": GLib.Variant("d", 1.0),
            "MaximumRate": GLib.Variant("d", 1.0),
            "Metadata": GLib.Variant("a{sv}", meta),
            "Volume": GLib.Variant("d", st.get("volume", 0) / 100.0),
            "Position": GLib.Variant("x", int(st.get("time", 0) * 1e6)),
            "CanGoNext": GLib.Variant("b", has), "CanGoPrevious": GLib.Variant("b", has),
            "CanPlay": GLib.Variant("b", has), "CanPause": GLib.Variant("b", has),
            "CanSeek": GLib.Variant("b", st.get("duration", 0) > 0),
            "CanControl": GLib.Variant("b", True),
        }

    def _get(self, _conn, _sender, _path, iface, prop):
        return self._props(iface).get(prop)

    def _set(self, _conn, _sender, _path, _iface, prop, value):
        if prop == "Volume":
            self.win.command(lambda p: p.set_volume(int(value.get_double() * 100)))
        return True

    def _call(self, _conn, _sender, _path, iface, method, params, invocation):
        win = self.win
        if iface == ROOT:
            if method == "Raise":
                win.present()
            elif method == "Quit":
                win.get_application().quit()
        elif method == "Next":
            win.command(lambda p: p.next())
        elif method == "Previous":
            win.command(lambda p: p.prev())
        elif method == "Pause":
            win.command(lambda p: p.pause())
        elif method == "Play":
            win.command(lambda p: p.play())
        elif method == "PlayPause":
            win.command(lambda p: p.toggle())
        elif method == "Stop":
            win.command(lambda p: p.stop())
        elif method == "Seek":
            offset = params.unpack()[0] / 1e6
            now = (self.st or {}).get("time", 0)
            win.command(lambda p: p.seek(max(0, now + offset)))
        elif method == "SetPosition":
            position = params.unpack()[1] / 1e6
            win.command(lambda p: p.seek(position))
        invocation.return_value(None)

    # ----------------------------------------------------------------- update

    def update(self, st):
        self.st = st
        if self.conn is None:
            return
        key = (st.get("mode"), st.get("title"), st.get("artist"), st.get("art"),
               st.get("volume"), bool(st.get("count")), int(st.get("duration", 0)))
        if key == self._last:
            return
        self._last = key
        props = self._props(PLAYER)
        props.pop("Position", None)
        try:
            self.conn.emit_signal(
                None, PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (PLAYER, props, [])))
        except GLib.Error:
            pass
