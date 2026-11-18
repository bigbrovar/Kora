"""Main window: connect page, sidebar, browse stack, queue and player bar."""

import time

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from . import APP_ID, APP_NAME, __version__
from .lms import LMSClient, LMSError, discover
from .mpris import Mpris
from .pages import BrowsePage, CollectionPage, Cover, ItemObject, SearchPage, _flat_button, _label
from .players import LMSPlayer, LocalPlayer, Squeezelite, empty_status
from .util import ArtCache, fmt_time, run_async

CSS = """
.cover { border-radius: 9px; background-color: alpha(currentColor, 0.08); }
.cover-small { border-radius: 5px; background-color: alpha(currentColor, 0.08); }
.playerbar { padding: 6px 10px; }
.playerbar scale trough, .playerbar scale highlight { border-radius: 99px; }
.browse-list > row, .album-grid > child { transition: background-color 120ms; }
.playerbar .now-title { font-weight: bold; }
.queue-current { color: @accent_color; font-weight: bold; }
.album-grid { background: none; }
.album-grid > child { border-radius: 12px; padding: 0; }
.browse-list { background: none; }
.browse-list > row { border-radius: 8px; margin: 0 6px; }
button.on { color: @accent_color; }
"""

SECTIONS = [
    ("albums", "Albums", "media-optical-symbolic"),
    ("artists", "Artists", "avatar-default-symbolic"),
    ("new", "New Music", "document-open-recent-symbolic"),
    ("random", "Random Albums", "media-playlist-shuffle-symbolic"),
    ("genres", "Genres", "folder-music-symbolic"),
    ("years", "Years", "x-office-calendar-symbolic"),
    ("playlists", "Playlists", "view-list-symbolic"),
    ("favorites", "Favorites", "starred-symbolic"),
    ("radio", "Radio", "network-wireless-symbolic"),
    ("apps", "Apps", "view-app-grid-symbolic"),
    ("search", "Search", "system-search-symbolic"),
]


class KoraWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.cfg = app.cfg
        self.art = ArtCache()
        self.client = None
        self.server_info = {}
        self.players = []
        self.player = None
        self.local = None
        self.squeeze = Squeezelite()
        self.st = empty_status()
        self.queue_items = []
        self._polling = False
        self._fails = 0
        self._players_sig = None
        self._queue_sig = None
        self._bar_art = None
        self._seek_hold = 0.0
        self._vol_hold = 0.0
        self._seek_source = 0
        self._vol_source = 0
        self._setting = False
        self._want_player_name = ""
        self._search_page = None

        self.skip = set(getattr(app, "skip", ()))     # optional parts to leave out
        self.probe = bool(getattr(app, "probe", False))
        self.set_default_size(self.cfg.get("width"), self.cfg.get("height"))
        self.set_size_request(400, 440)
        if "css" not in self.skip:
            self._optional("custom styling", self._load_css)

        self.root = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.root.add_named(self._build_connect_page(), "connect")
        self.root.add_named(self._build_main(), "main")
        self.toasts = Adw.ToastOverlay(child=self.root)
        self.set_content(self.toasts)

        if "breakpoints" not in self.skip:
            self._optional("adaptive layout", self._add_breakpoints)

        self._install_actions()
        self.mpris = None
        if "mpris" not in self.skip:
            self._optional("media key support", self._start_mpris)
        self.connect("close-request", self._on_close)
        GLib.timeout_add(1000, self._tick)
        GLib.timeout_add_seconds(8, self._tick_players)

        if self.cfg.get("host"):
            self.connect_to(self.cfg.get("host"), self.cfg.get("port"),
                            self.cfg.get("user"), self.cfg.get("password"))
        else:
            self._discover()

    @staticmethod
    def _optional(what, fn):
        """Extras must never stop the window from opening."""
        try:
            fn()
        except Exception:
            import sys
            import traceback
            print(f"kora: {what} is unavailable on this system:", file=sys.stderr)
            traceback.print_exc()

    def _load_css(self):
        provider = Gtk.CssProvider()
        if hasattr(provider, "load_from_string"):
            provider.load_from_string(CSS)
        else:
            provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _start_mpris(self):
        self.mpris = Mpris(self)

    def _add_breakpoints(self):
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 820sp"))
        narrow.add_setter(self.split, "collapsed", True)
        narrow.add_setter(self.queue_split, "collapsed", True)
        for widget in (self.shuffle_btn, self.repeat_btn, self.pos_label, self.dur_label):
            narrow.add_setter(widget, "visible", False)
        self.add_breakpoint(narrow)
        tiny = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 560sp"))
        tiny.add_setter(self.split, "collapsed", True)
        tiny.add_setter(self.queue_split, "collapsed", True)
        for widget in (self.shuffle_btn, self.repeat_btn, self.pos_label, self.dur_label,
                       self.prev_btn, self.volume, self.seek):
            tiny.add_setter(widget, "visible", False)
        tiny.add_setter(self.now_box, "hexpand", True)
        self.add_breakpoint(tiny)

    # ===================================================================== UI

    def _install_actions(self):
        def add(name, callback, accels=()):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a: callback())
            self.add_action(action)
            if accels:
                self.get_application().set_accels_for_action(f"win.{name}", list(accels))
        add("preferences", self._show_preferences, ("<Control>comma",))
        add("rescan", self._rescan)
        add("disconnect", self.disconnect_server)
        add("random-tracks", lambda: self._random_mix("tracks"))
        add("random-albums", lambda: self._random_mix("albums"))
        add("search", lambda: self._select_section("search"), ("<Control>f",))
        add("play-pause", lambda: self.command(lambda p: p.toggle()), ("<Control>space",))
        add("next", lambda: self.command(lambda p: p.next()), ("<Control>Right",))
        add("previous", lambda: self.command(lambda p: p.prev()), ("<Control>Left",))
        add("queue", lambda: self.queue_btn.set_active(not self.queue_btn.get_active()), ("F9",))

        # View > Grid / List for album pages: a radio pair in the main menu.
        current = self.cfg.get("album_view")
        view = Gio.SimpleAction.new_stateful(
            "album-view", GLib.VariantType.new("s"),
            GLib.Variant("s", current if current in ("grid", "list") else "grid"))
        view.connect("activate", self._album_view_chosen)
        self.add_action(view)
        app = self.get_application()
        app.set_accels_for_action("win.album-view::list", ["<Control>1"])
        app.set_accels_for_action("win.album-view::grid", ["<Control>2"])

    def _album_view_chosen(self, action, value):
        layout = value.get_string()
        if layout not in ("grid", "list"):
            return
        action.set_state(value)
        self.cfg.set("album_view", layout)
        stack = self.nav.get_navigation_stack()
        for index in range(stack.get_n_items()):
            page = stack.get_item(index)
            if hasattr(page, "apply_layout"):
                page.apply_layout(layout)

    # --------------------------------------------------------- connect page

    def _build_connect_page(self):
        self.found_group = Adw.PreferencesGroup(title="Found on your network")
        refresh = _flat_button("view-refresh-symbolic", "Search again")
        refresh.connect("clicked", lambda _b: self._discover())
        self.found_group.set_header_suffix(refresh)
        self._found_rows = []
        self.found_hint = Adw.ActionRow(title="Searching…")
        self.found_group.add(self.found_hint)

        manual = Adw.PreferencesGroup(title="Server")
        self.host_row = Adw.EntryRow(title="Address")
        self.host_row.set_text(self.cfg.get("host"))
        self.port_row = Adw.EntryRow(title="Port")
        self.port_row.set_text(str(self.cfg.get("port")))
        self.port_row.set_input_purpose(Gtk.InputPurpose.DIGITS)
        self.user_row = Adw.EntryRow(title="Username (only if the server is protected)")
        self.user_row.set_text(self.cfg.get("user"))
        self.pass_row = Adw.PasswordEntryRow(title="Password")
        self.pass_row.set_text(self.cfg.get("password"))
        for row in (self.host_row, self.port_row, self.user_row, self.pass_row):
            row.connect("entry-activated", lambda _r: self._connect_clicked())
            manual.add(row)

        self.connect_btn = Gtk.Button(label="Connect", halign=Gtk.Align.CENTER)
        self.connect_btn.add_css_class("suggested-action")
        self.connect_btn.add_css_class("pill")
        self.connect_btn.connect("clicked", lambda _b: self._connect_clicked())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        box.append(self.found_group)
        box.append(manual)
        box.append(self.connect_btn)
        page = Adw.StatusPage(
            icon_name=APP_ID, title="Connect to your music server",
            description="Lyrion Music Server, formerly Logitech Media Server",
            child=Adw.Clamp(maximum_size=460, child=box))
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(page)
        return toolbar

    def _discover(self):
        self.found_hint.set_title("Searching…")
        self.found_hint.set_visible(True)
        run_async(discover, self._discovered, lambda _e: self._discovered([]))

    def _discovered(self, servers):
        for row in self._found_rows:
            self.found_group.remove(row)
        self._found_rows = []
        self.found_hint.set_visible(not servers)
        self.found_hint.set_title("No servers answered. Enter the address below.")
        for s in servers:
            row = Adw.ActionRow(title=GLib.markup_escape_text(s["name"]),
                                subtitle=f"{s['host']}:{s['port']}", activatable=True)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, srv=s: self._pick_server(srv))
            self.found_group.add(row)
            self._found_rows.append(row)

    def _pick_server(self, server):
        self.host_row.set_text(server["host"])
        self.port_row.set_text(str(server["port"]))
        self._connect_clicked()

    def _connect_clicked(self):
        host = self.host_row.get_text().strip()
        if not host:
            self.toast("Enter the server address")
            return
        try:
            port = int(self.port_row.get_text().strip() or 9000)
        except ValueError:
            self.toast("The port must be a number")
            return
        self.connect_to(host, port, self.user_row.get_text().strip(), self.pass_row.get_text())

    # ------------------------------------------------------------ main view

    def _build_main(self):
        self.nav = Adw.NavigationView()
        self.split = Adw.OverlaySplitView(sidebar=self._build_sidebar(), content=self.nav)
        self.split.set_min_sidebar_width(190)
        self.split.set_max_sidebar_width(230)
        self.queue_split = Adw.OverlaySplitView(
            sidebar=self._build_queue(), content=self.split,
            sidebar_position=Gtk.PackType.END, show_sidebar=False)
        self.queue_split.set_min_sidebar_width(290)
        self.queue_split.set_max_sidebar_width(360)
        main = Adw.ToolbarView()
        main.set_content(self.queue_split)
        main.add_bottom_bar(self._build_player_bar())
        return main

    def make_sidebar_toggle(self):
        button = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", tooltip_text="Toggle sidebar")
        self.split.bind_property(
            "show-sidebar", button, "active",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        return button

    def _build_sidebar(self):
        menu = Gio.Menu()
        view = Gio.Menu()
        view.append("Grid", "win.album-view::grid")
        view.append("List", "win.album-view::list")
        view_section = Gio.Menu()
        view_section.append_submenu("View", view)
        menu.append_section(None, view_section)
        mix = Gio.Menu()
        mix.append("Play Random Songs", "win.random-tracks")
        mix.append("Play Random Albums", "win.random-albums")
        menu.append_section(None, mix)
        server = Gio.Menu()
        server.append("Rescan Library", "win.rescan")
        server.append("Disconnect", "win.disconnect")
        menu.append_section(None, server)
        about = Gio.Menu()
        about.append("Preferences", "win.preferences")
        about.append(f"About {APP_NAME}", "app.about")
        menu.append_section(None, about)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=APP_NAME))
        self.menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text="Main menu", primary=True)
        header.pack_end(self.menu_btn)

        self.section_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.section_list.add_css_class("navigation-sidebar")
        for key, label, icon in SECTIONS:
            row = Gtk.ListBoxRow()
            row.key = key
            box = Gtk.Box(spacing=12)
            box.append(Gtk.Image(icon_name=icon))
            box.append(Gtk.Label(label=label, xalign=0))
            row.set_child(box)
            self.section_list.append(row)
        self.section_list.connect("row-selected", self._section_selected)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(Gtk.ScrolledWindow(child=self.section_list))
        return toolbar

    def _build_queue(self):
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        self.queue_title = Adw.WindowTitle(title="Queue")
        header.set_title_widget(self.queue_title)
        clear = Gtk.Button(icon_name="edit-clear-all-symbolic", tooltip_text="Clear queue")
        clear.connect("clicked", lambda _b: self.command(lambda p: p.queue_clear()))
        header.pack_end(clear)

        self.queue_store = Gio.ListStore(item_type=ItemObject)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._queue_setup)
        factory.connect("bind", self._queue_bind)
        self.queue_view = Gtk.ListView(model=Gtk.NoSelection(model=self.queue_store),
                                       factory=factory)
        self.queue_view.add_css_class("browse-list")
        self.queue_view.set_single_click_activate(True)
        self.queue_view.connect(
            "activate", lambda _v, pos: self.command(lambda p: p.queue_jump(pos)))
        self.queue_empty = Adw.StatusPage(icon_name="view-list-symbolic", title="Queue is empty",
                                          description="Play or add something from the library")
        self.queue_empty.add_css_class("compact")
        self.queue_stack = Gtk.Stack()
        self.queue_stack.add_named(self.queue_empty, "empty")
        scroller = Gtk.ScrolledWindow(child=self.queue_view)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.queue_stack.add_named(scroller, "list")

        # Don't Stop The Music: a server plugin that keeps adding similar
        # tracks when the queue runs out. Set per player.
        self.dstm_label = _label(label="Don't Stop the Music")
        self.dstm_state = _label(("dim-label", "caption"), label="")
        dstm_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        dstm_text.append(self.dstm_label)
        dstm_text.append(self.dstm_state)
        dstm_row = Gtk.Box(spacing=10)
        dstm_row.append(Gtk.Image(icon_name="media-playlist-consecutive-symbolic"))
        dstm_row.append(dstm_text)
        self.dstm_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                                margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        self.dstm_pop = Gtk.Popover(child=self.dstm_box)
        self.dstm_pop.connect("show", lambda _p: self._dstm_refresh(fill=True))
        self.dstm_btn = Gtk.MenuButton(child=dstm_row, popover=self.dstm_pop, hexpand=True,
                                       tooltip_text="Keep playing similar music when the queue ends",
                                       margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        self.dstm_btn.add_css_class("flat")

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.queue_stack)
        toolbar.add_bottom_bar(self.dstm_btn)
        return toolbar

    def _dstm_refresh(self, fill=False):
        """Update the Don't Stop The Music label, and the popover list if open."""
        player = self.player
        if player is None:
            return

        def loaded(options):
            if player is not self.player:
                return
            self.dstm_btn.set_sensitive(True)
            current = next((o for o in options if o["selected"]), None)
            is_on = current is not None and current is not options[0]
            self.dstm_state.set_label(current["text"] if is_on else "Off")
            if not fill:
                return
            self._dstm_fill(player, options)

        def failed(exc):
            if player is not self.player:
                return
            self.dstm_state.set_label("Needs an LMS player" if player.is_local else "Unavailable")
            self.dstm_btn.set_sensitive(False)
            self.dstm_btn.set_tooltip_text(
                str(exc) if player.is_local else
                "Enable the Don't Stop The Music plugin in the server settings")
        run_async(player.dstm_options, loaded, failed)

    def _dstm_fill(self, player, options):
        child = self.dstm_box.get_first_child()
        while child is not None:
            self.dstm_box.remove(child)
            child = self.dstm_box.get_first_child()
        group = None
        for option in options:
            check = Gtk.CheckButton(label=option["text"], active=option["selected"])
            if group is None:
                group = check
            else:
                check.set_group(group)
            check.connect("toggled", self._dstm_chosen, player, option)
            self.dstm_box.append(check)

    def _dstm_chosen(self, check, player, option):
        if not check.get_active():
            return
        self.dstm_pop.popdown()
        run_async(lambda: player.dstm_set(option["cmd"]),
                  lambda _r: self._dstm_refresh(), self.toast_error)

    def _queue_setup(self, _factory, li):
        box = Gtk.Box(spacing=10, margin_top=4, margin_bottom=4, margin_start=8, margin_end=4)
        cover = Cover(38, "cover-small")
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER,
                       hexpand=True, spacing=1)
        title = _label()
        sub = _label(("dim-label", "caption"))
        text.append(title)
        text.append(sub)
        remove = _flat_button("window-close-symbolic", "Remove from queue")
        remove.connect("clicked", lambda _b: self._queue_remove(li.get_position()))
        for widget in (cover, text, remove):
            box.append(widget)
        box.parts = (cover, title, sub)
        li.set_child(box)

    def _queue_bind(self, _factory, li):
        item = li.get_item().item
        cover, title, sub = li.get_child().parts
        title.set_label(item.title)
        sub.set_label(item.extra.get("artist") or item.subtitle)
        if li.get_position() == self.st["index"] and self.st["count"]:
            title.add_css_class("queue-current")
        else:
            title.remove_css_class("queue-current")
        cover.show_art(self.art, item.art)

    def _queue_remove(self, position):
        self.command(lambda p: p.queue_remove(position))

    def _build_player_bar(self):
        self.bar_cover = Cover(52, "cover-small")
        self.now_title = _label(("now-title",), label="Nothing playing")
        self.now_artist = _label(("dim-label", "caption"))
        now = self.now_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                     valign=Gtk.Align.CENTER, spacing=2)
        now.set_size_request(100, -1)
        now.append(self.now_title)
        now.append(self.now_artist)
        self.now_title.set_max_width_chars(26)
        self.now_artist.set_max_width_chars(26)

        prev = self.prev_btn = _flat_button("media-skip-backward-symbolic", "Previous")
        prev.connect("clicked", lambda _b: self.command(lambda p: p.prev()))
        self.play_btn = Gtk.Button(icon_name="media-playback-start-symbolic",
                                   tooltip_text="Play or pause", valign=Gtk.Align.CENTER)
        self.play_btn.add_css_class("circular")
        self.play_btn.add_css_class("suggested-action")
        self.play_btn.connect("clicked", lambda _b: self.command(lambda p: p.toggle()))
        nxt = _flat_button("media-skip-forward-symbolic", "Next")
        nxt.connect("clicked", lambda _b: self.command(lambda p: p.next()))

        self.pos_label = _label(("dim-label", "numeric", "caption"), label="0:00")
        self.dur_label = _label(("dim-label", "numeric", "caption"), label="0:00")
        self.seek = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.seek.set_draw_value(False)
        self.seek.set_hexpand(True)
        self.seek.set_size_request(90, -1)
        self.seek.set_sensitive(False)
        self.seek.connect("change-value", self._seek_changed)

        self.shuffle_btn = _flat_button("media-playlist-shuffle-symbolic", "Shuffle")
        self.shuffle_btn.connect("clicked", self._cycle_shuffle)
        self.repeat_btn = _flat_button("media-playlist-repeat-symbolic", "Repeat")
        self.repeat_btn.connect("clicked", self._cycle_repeat)

        self.volume = Gtk.ScaleButton.new(0, 100, 2, [
            "audio-volume-muted-symbolic", "audio-volume-high-symbolic",
            "audio-volume-low-symbolic", "audio-volume-medium-symbolic"])
        self.volume.set_tooltip_text("Volume")
        self.volume.set_valign(Gtk.Align.CENTER)
        self.volume.connect("value-changed", self._volume_changed)

        self.player_content = Adw.ButtonContent(icon_name="audio-speakers-symbolic",
                                                label="No player", can_shrink=True)
        self.player_pop = Gtk.Popover()
        self.player_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.player_box.add_css_class("boxed-list")
        self.player_box.connect("row-activated", self._player_row_activated)
        pop_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                           margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        heading = Gtk.Label(label="Play on", xalign=0)
        heading.add_css_class("heading")
        pop_body.append(heading)
        pop_body.append(self.player_box)
        self.player_pop.set_child(pop_body)
        self.player_btn = Gtk.MenuButton(child=self.player_content, popover=self.player_pop,
                                         tooltip_text="Choose player", valign=Gtk.Align.CENTER)
        self.player_btn.add_css_class("flat")

        self.queue_btn = Gtk.ToggleButton(icon_name="view-list-symbolic", tooltip_text="Queue",
                                          valign=Gtk.Align.CENTER)
        self.queue_btn.add_css_class("flat")
        self.queue_split.bind_property(
            "show-sidebar", self.queue_btn, "active",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)

        bar = Gtk.Box(spacing=6)
        bar.add_css_class("playerbar")
        for widget in (self.bar_cover, now, prev, self.play_btn, nxt, self.pos_label, self.seek,
                       self.dur_label, self.shuffle_btn, self.repeat_btn, self.volume,
                       self.player_btn, self.queue_btn):
            bar.append(widget)
        now.set_margin_start(6)
        now.set_margin_end(6)
        return bar

    # ============================================================= connection

    def connect_to(self, host, port, user="", password=""):
        client = LMSClient(host, port, user, password)
        self.connect_btn.set_sensitive(False)
        self.connect_btn.set_label("Connecting…")
        run_async(client.server_status,
                  lambda info: self._connected(client, info),
                  self._connect_failed)

    def _connect_failed(self, exc):
        self.connect_btn.set_sensitive(True)
        self.connect_btn.set_label("Connect")
        self.root.set_visible_child_name("connect")
        self.toast(str(exc))
        self._discover()

    def _connected(self, client, info):
        self.connect_btn.set_sensitive(True)
        self.connect_btn.set_label("Connect")
        self.client = client
        self.art.set_client(client)
        for key, value in (("host", client.host), ("port", client.port),
                           ("user", client.user), ("password", client.password)):
            self.cfg.set(key, value, save=False)
        self.cfg.save()

        if self.local is not None:
            self.local.shutdown()
        try:
            self.local = LocalPlayer(client, self.cfg.get("local_volume"),
                                     on_error=lambda msg: GLib.idle_add(self.toast, msg))
        except Exception as exc:  # GStreamer missing: keep working as a controller
            self.local = None
            self.toast(f"Local playback unavailable: {exc}")
        self._start_squeezelite()

        self._players_sig = None
        self.player = None
        self._on_server_status(info)
        self.root.set_visible_child_name("main")
        first = self.section_list.get_row_at_index(0)
        self.section_list.unselect_all()
        self.section_list.select_row(first)

    def _start_squeezelite(self):
        self.squeeze.stop()
        if self.cfg.get("squeezelite") and self.client is not None and not self.probe:
            name = self.cfg.get("squeezelite_name") or Squeezelite.default_name()
            if self.squeeze.start(self.client.host, name):
                self._want_player_name = name
                GLib.timeout_add(1500, lambda: (self._tick_players(), False)[1])

    def disconnect_server(self):
        self.squeeze.stop()
        if self.local is not None:
            self.local.shutdown()
            self.local = None
        self.client = None
        self.players = []
        self.player = None
        self.cfg.set("host", "")
        self.host_row.set_text("")
        self.nav.replace([])
        self._search_page = None
        self.root.set_visible_child_name("connect")
        self._discover()

    # ================================================================ players

    def _tick_players(self):
        client = self.client
        if client is not None:
            run_async(client.server_status,
                      lambda info: client is self.client and self._on_server_status(info),
                      lambda _exc: None)
        return True

    def _on_server_status(self, info):
        self.server_info = info
        sig = [(p["id"], p["name"], p["connected"], p["power"]) for p in info["players"]]
        if sig == self._players_sig:
            return
        self._players_sig = sig
        wanted = self.player.id if self.player is not None else self.cfg.get("player")
        self.players = [LMSPlayer(self.client, p) for p in info["players"]]
        if self.local is not None:
            self.players.append(self.local)
        chosen = None
        if self._want_player_name:
            chosen = next((p for p in self.players if p.name == self._want_player_name), None)
            if chosen is not None:
                self._want_player_name = ""
        if chosen is None:
            chosen = next((p for p in self.players if p.id == wanted), None)
        if chosen is None:
            chosen = next((p for p in self.players if p.connected and not p.is_local), None)
        if chosen is None and self.players:
            chosen = self.players[0]
        same = self.player is not None and chosen is not None and chosen.id == self.player.id
        self.player = chosen
        if chosen is None:
            self.player_content.set_label("No player")
        elif not same:
            self._select_player(chosen)
            return
        self._rebuild_player_menu()

    def _select_player(self, player):
        self.player = player
        self.cfg.set("player", player.id)
        self.player_content.set_label(player.name)
        self._queue_sig = None
        self._bar_art = None
        self._rebuild_player_menu()
        self._dstm_refresh()
        self._poll()

    def _rebuild_player_menu(self):
        self.player_box.remove_all()
        current = self.player
        for p in self.players:
            row = Gtk.ListBoxRow(activatable=True)
            row.player = p
            box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6, margin_start=10, margin_end=6)
            check = Gtk.Image(icon_name="object-select-symbolic")
            check.set_opacity(1.0 if current is not None and p.id == current.id else 0.0)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                           valign=Gtk.Align.CENTER)
            text.append(_label(label=p.name, max_width_chars=28))
            detail = p.model if p.connected else "Disconnected"
            text.append(_label(("dim-label", "caption"), label=detail))
            box.append(check)
            box.append(text)
            if (not p.is_local and current is not None and not current.is_local
                    and p.id != current.id):
                sync = Gtk.ToggleButton(label="Sync", valign=Gtk.Align.CENTER,
                                        tooltip_text=f"Play in sync with {current.name}")
                sync.set_active(p.id in self.st.get("sync", []))
                sync.connect("toggled", self._sync_toggled, p)
                box.append(sync)
            if not p.is_local:
                power = Gtk.ToggleButton(icon_name="system-shutdown-symbolic",
                                         valign=Gtk.Align.CENTER, tooltip_text="Power")
                power.add_css_class("flat")
                power.set_active(p.power)
                power.connect("toggled", self._power_toggled, p)
                box.append(power)
            row.set_child(box)
            self.player_box.append(row)

    def _player_row_activated(self, _box, row):
        self.player_pop.popdown()
        if self.player is None or row.player.id != self.player.id:
            self.st = empty_status()
            self._select_player(row.player)

    def _sync_toggled(self, button, other):
        master = self.player
        if button.get_active():
            work = lambda: master.sync_add(other.id)
        else:
            work = other.unsync
        run_async(work, lambda _r: self.poll_soon(), self.toast_error)

    def _power_toggled(self, button, player):
        on = button.get_active()
        player.power = on
        run_async(lambda: player.set_power(on), lambda _r: self.poll_soon(), self.toast_error)

    # =============================================================== commands

    def toast(self, message):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(str(message)), timeout=4))
        return False

    def toast_error(self, exc):
        self.toast(str(exc) if isinstance(exc, LMSError) else f"{type(exc).__name__}: {exc}")

    def command(self, fn):
        """Run fn(player) for the active player off the main thread."""
        player = self.player
        if player is None:
            self.toast("No player selected")
            return
        run_async(lambda: fn(player), lambda _r: self.poll_soon(), self.toast_error)

    def play_item(self, item, mode="load", index=None):
        player = self.player
        if player is None:
            self.toast("No player selected")
            return

        def done(_result):
            self.poll_soon()
            if mode == "add":
                self.toast(f"Added to queue: {item.title}")
            elif mode == "insert":
                self.toast(f"Playing next: {item.title}")
        run_async(lambda: player.play_item(item, mode, index), done, self.toast_error)

    def _random_mix(self, what):
        self.command(lambda p: p.random_mix(what))

    def _rescan(self):
        if self.client is None:
            return
        run_async(self.client.rescan, lambda _r: self.toast("Library rescan started"),
                  self.toast_error)

    def _cycle_shuffle(self, _button):
        mode = 0 if self.st["shuffle"] else 1
        self.command(lambda p: p.set_shuffle(mode))

    def _cycle_repeat(self, _button):
        mode = {0: 2, 2: 1, 1: 0}.get(self.st["repeat"], 0)
        self.command(lambda p: p.set_repeat(mode))

    def _seek_changed(self, _scale, _scroll, value):
        self._seek_hold = time.monotonic() + 1.5
        target = max(0.0, min(value, self.st["duration"] or value))
        self.pos_label.set_label(fmt_time(target))
        if self._seek_source:
            GLib.source_remove(self._seek_source)

        def fire():
            self._seek_source = 0
            self.command(lambda p: p.seek(target))
            return False
        self._seek_source = GLib.timeout_add(200, fire)
        return False

    def _volume_changed(self, _button, value):
        if self._setting:
            return
        self._vol_hold = time.monotonic() + 1.5
        if self._vol_source:
            GLib.source_remove(self._vol_source)

        def fire():
            self._vol_source = 0
            level = int(value)
            if self.player is not None and self.player.is_local:
                self.cfg.set("local_volume", level)
            self.command(lambda p: p.set_volume(level))
            return False
        self._vol_source = GLib.timeout_add(120, fire)

    # ================================================================ polling

    def _tick(self):
        self._poll()
        return True

    def poll_soon(self):
        GLib.timeout_add(150, lambda: (self._poll(), False)[1])

    def _poll(self):
        player = self.player
        if self._polling or player is None or self.client is None:
            return
        self._polling = True
        run_async(player.status,
                  lambda st: self._on_status(player, st),
                  lambda exc: self._on_status_failed(exc))

    def _on_status_failed(self, exc):
        self._polling = False
        self._fails += 1
        if self._fails == 3:
            self.toast(f"Lost contact with the server: {exc}")

    def _on_status(self, player, st):
        self._polling = False
        if self._fails >= 3:
            self.toast("Reconnected")
        self._fails = 0
        if player is not self.player:
            return
        old, self.st = self.st, st
        now = time.monotonic()

        has_track = bool(st["count"]) and bool(st["title"])
        self.now_title.set_label(st["title"] if has_track else "Nothing playing")
        self.now_artist.set_label(" · ".join(x for x in (st["artist"], st["album"]) if x)
                                  if has_track else player.name)
        art = st["art"] if has_track else ""
        if art != self._bar_art:
            self._bar_art = art
            self.bar_cover.show_art(self.art, art)
        self.play_btn.set_icon_name("media-playback-pause-symbolic" if st["mode"] == "play"
                                    else "media-playback-start-symbolic")

        duration = st["duration"]
        if now > self._seek_hold:
            self.seek.set_range(0, max(duration, 1))
            self.seek.set_value(min(st["time"], max(duration, 1)))
            self.pos_label.set_label(fmt_time(st["time"]))
        self.seek.set_sensitive(duration > 0)
        self.dur_label.set_label(fmt_time(duration))

        if now > self._vol_hold and int(self.volume.get_value()) != st["volume"]:
            self._setting = True
            self.volume.set_value(st["volume"])
            self._setting = False

        self._mark(self.shuffle_btn, st["shuffle"] > 0)
        self._mark(self.repeat_btn, st["repeat"] > 0)
        self.repeat_btn.set_icon_name("media-playlist-repeat-song-symbolic" if st["repeat"] == 1
                                      else "media-playlist-repeat-symbolic")
        self.repeat_btn.set_tooltip_text(
            {0: "Repeat is off", 1: "Repeating this song", 2: "Repeating the queue"}
            .get(st["repeat"], "Repeat"))
        self.shuffle_btn.set_tooltip_text("Shuffle is on" if st["shuffle"] else "Shuffle is off")

        sig = (player.id, st["stamp"], st["count"])
        if sig != self._queue_sig:
            self._queue_sig = sig
            self._load_queue(player)
        elif st["index"] != old["index"]:
            for pos in {old["index"], st["index"]}:
                if 0 <= pos < self.queue_store.get_n_items():
                    self.queue_store.items_changed(pos, 1, 1)
        if st["sync"] != old["sync"]:
            self._rebuild_player_menu()
        if self.mpris is not None:
            self.mpris.update(st)

    @staticmethod
    def _mark(button, on):
        if on:
            button.add_css_class("on")
        else:
            button.remove_css_class("on")

    def _load_queue(self, player):
        def loaded(items):
            if player is not self.player:
                return
            self.queue_items = items
            self.queue_store.remove_all()
            if items:
                self.queue_store.splice(0, 0, [ItemObject(i) for i in items])
            self.queue_stack.set_visible_child_name("list" if items else "empty")
            count = self.st["count"]
            self.queue_title.set_subtitle(
                f"{count} song{'s' if count != 1 else ''}" if count else "")
        run_async(player.queue, loaded, lambda _exc: None)

    # ============================================================= navigation

    def _select_section(self, key):
        if self.client is None:
            return
        for index, (name, _label_text, _icon) in enumerate(SECTIONS):
            if name == key:
                row = self.section_list.get_row_at_index(index)
                if self.section_list.get_selected_row() is row:
                    self._section_selected(self.section_list, row)
                else:
                    self.section_list.select_row(row)

    def _section_selected(self, _list, row):
        if row is None or self.client is None:
            return
        c = self.client
        key = row.key
        if key == "albums":
            page = BrowsePage(self, "Albums", c.albums, "grid", limit=20000)
        elif key == "artists":
            page = BrowsePage(self, "Artists", c.artists, limit=20000)
        elif key == "new":
            page = BrowsePage(self, "New Music",
                              lambda s, n: c.albums(s, min(n, 100), sort="new"), "grid", limit=100)
        elif key == "random":
            page = BrowsePage(self, "Random Albums",
                              lambda s, n: c.albums(s, min(n, 60), sort="random"), "grid", limit=60)
        elif key == "genres":
            page = BrowsePage(self, "Genres", c.genres)
        elif key == "years":
            page = BrowsePage(self, "Years", c.years)
        elif key == "playlists":
            page = BrowsePage(self, "Playlists", c.playlists)
        elif key == "favorites":
            page = BrowsePage(self, "Favorites", c.favorites)
        elif key == "radio":
            page = BrowsePage(self, "Radio", c.radios)
        elif key == "apps":
            page = BrowsePage(self, "Apps", c.apps)
        else:
            if self._search_page is None:
                self._search_page = SearchPage(self)
            page = self._search_page
        self.nav.replace([page])
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def _browse_player(self):
        p = self.player
        return p.id if p is not None and not p.is_local else ""

    def open_item(self, item):
        c = self.client
        if c is None:
            return
        kind = item.kind
        if kind == "artist":
            page = BrowsePage(self, item.title,
                              lambda s, n: c.albums(s, n, artist_id=item.id, sort="yearalbum"),
                              "grid", actions_item=item)
        elif kind == "genre":
            page = BrowsePage(self, item.title, lambda s, n: c.albums(s, n, genre_id=item.id),
                              "grid", actions_item=item)
        elif kind == "year":
            page = BrowsePage(self, item.title, lambda s, n: c.albums(s, n, year=item.id),
                              "grid", actions_item=item)
        elif kind == "album":
            page = CollectionPage(self, item, lambda: c.tracks(0, 2000, album_id=item.id)[0])
        elif kind == "playlist":
            page = CollectionPage(self, item, lambda: c.playlist_tracks(item.id, 0, 2000)[0])
        elif kind == "plugin" and "search" in item.extra.get("type", ""):
            self._plugin_search(item)
            return
        elif kind == "plugin" and item.browsable:
            cmd, player = item.extra["cmd"], self._browse_player()
            page = BrowsePage(self, item.title,
                              lambda s, n: c.plugin_items(cmd, s, n, item_id=item.id or None,
                                                          player=player))
        elif item.playable:
            self.play_item(item, "load")
            return
        else:
            return
        self.nav.push(page)

    def _plugin_search(self, item):
        entry = Gtk.Entry(activates_default=True, placeholder_text="Search term")
        dialog = Adw.AlertDialog(heading=item.title)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("go", "Search")
        dialog.set_default_response("go")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)

        def answered(_dialog, response):
            term = entry.get_text().strip()
            if response != "go" or not term or self.client is None:
                return
            c, cmd, player = self.client, item.extra["cmd"], self._browse_player()
            self.nav.push(BrowsePage(
                self, f"{item.title}: {term}",
                lambda s, n: c.plugin_items(cmd, s, n, item_id=item.id or None,
                                            search=term, player=player)))
        dialog.connect("response", answered)
        dialog.present(self)

    # ============================================================ preferences

    def _show_preferences(self):
        dialog = Adw.PreferencesDialog(title="Preferences")
        page = Adw.PreferencesPage()

        server = Adw.PreferencesGroup(title="Server")
        if self.client is not None:
            info = self.server_info
            row = Adw.ActionRow(
                title=f"{self.client.host}:{self.client.port}",
                subtitle=(f"Lyrion Music Server {info.get('version', '')} · "
                          f"{info.get('albums', 0)} albums · {info.get('songs', 0)} songs"))
            leave = Gtk.Button(label="Disconnect", valign=Gtk.Align.CENTER)
            leave.connect("clicked", lambda _b: (dialog.close(), self.disconnect_server()))
            row.add_suffix(leave)
        else:
            row = Adw.ActionRow(title="Not connected")
        server.add(row)
        page.add(server)

        local = Adw.PreferencesGroup(
            title="This computer",
            description="The built-in output plays library tracks. Squeezelite turns this "
                        "computer into a full LMS player with sync, radio and apps.")
        have = Squeezelite.binary() is not None
        switch = Adw.SwitchRow(
            title="Run Squeezelite while Kora is open",
            subtitle="" if have else "Squeezelite is not installed on this computer",
            sensitive=have, active=bool(self.cfg.get("squeezelite")) and have)
        name = Adw.EntryRow(title="Squeezelite player name", show_apply_button=True)
        name.set_text(self.cfg.get("squeezelite_name") or Squeezelite.default_name())
        name.set_sensitive(have)

        def toggled(row_, _pspec):
            self.cfg.set("squeezelite", row_.get_active())
            self._start_squeezelite()

        def renamed(row_):
            self.cfg.set("squeezelite_name", row_.get_text().strip())
            self._start_squeezelite()
        switch.connect("notify::active", toggled)
        name.connect("apply", renamed)
        local.add(switch)
        local.add(name)
        page.add(local)
        dialog.add(page)
        dialog.present(self)

    # =================================================================== exit

    def _on_close(self, _window):
        width, height = self.get_default_size()
        if width > 0 and height > 0 and not self.is_maximized():
            self.cfg.set("width", width, save=False)
            self.cfg.set("height", height, save=False)
        self.cfg.save()
        self.squeeze.stop()
        if self.local is not None:
            self.local.shutdown()
        return False


def show_about(window):
    Adw.AboutDialog(
        application_name=APP_NAME, application_icon=APP_ID,
        version=__version__, license_type=Gtk.License.GPL_3_0,
        developer_name="bigbrovar",
        website="https://github.com/bigbrovar/Kora",
        issue_url="https://github.com/bigbrovar/Kora/issues",
        comments="A GTK4 client for Lyrion Music Server (Logitech Media Server).\n"
                 "Browse the library, control every player and listen on this computer.",
    ).present(window)
