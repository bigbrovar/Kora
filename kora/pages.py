"""Browse pages: generic grid/list page, collection (album/playlist) page, search."""

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from .util import fmt_time, run_async

ICONS = {
    "artist": "avatar-default-symbolic",
    "album": "media-optical-symbolic",
    "track": "audio-x-generic-symbolic",
    "genre": "folder-music-symbolic",
    "year": "x-office-calendar-symbolic",
    "playlist": "view-list-symbolic",
    "plugin": "folder-symbolic",
}


def icon_for(item):
    if item.kind == "plugin":
        kind = item.extra.get("type", "")
        if "search" in kind:
            return "system-search-symbolic"
        if item.playable and not item.browsable:
            return "audio-x-generic-symbolic"
        if kind == "text":
            return "dialog-information-symbolic"
    return ICONS.get(item.kind, "audio-x-generic-symbolic")


class ItemObject(GObject.Object):
    """GObject wrapper so plain Items can live in a Gio.ListStore."""

    def __init__(self, item):
        super().__init__()
        self.item = item


class Cover(Gtk.Overlay):
    """Fixed-size artwork square with an icon placeholder underneath."""

    def __init__(self, size, css="cover"):
        super().__init__()
        self._want = None
        self.icon = Gtk.Image(pixel_size=max(16, size // 3))
        self.icon.add_css_class("dim-label")
        self.icon.set_size_request(size, size)
        self.set_child(self.icon)
        self.pic = Gtk.Picture(can_shrink=True)
        self.pic.set_content_fit(Gtk.ContentFit.COVER)
        self.add_overlay(self.pic)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.add_css_class(css)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)

    def show_art(self, cache, url, icon="audio-x-generic-symbolic"):
        self._want = url
        self.icon.set_from_icon_name(icon)
        tex = cache.get(url) if url else None
        self.pic.set_paintable(tex)
        if url and tex is None:
            def arrived(texture, wanted=url):
                if self._want == wanted and texture is not None:
                    self.pic.set_paintable(texture)
            cache.load(url, arrived)


def _label(css=(), **props):
    label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, **props)
    for name in css:
        label.add_css_class(name)
    return label


def _flat_button(icon, tooltip):
    button = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER)
    button.add_css_class("flat")
    return button


class BrowsePage(Adw.NavigationPage):
    """Lazy list or grid of Items fed by loader(start, count) -> (items, total)."""

    CHUNK = 200

    def __init__(self, win, title, loader, layout="list", limit=5000,
                 actions_item=None, autoload=True):
        super().__init__(title=title)
        self.win = win
        self.loader = loader
        self.layout = layout
        self.limit = limit
        self._gen = 0
        self.store = Gio.ListStore(item_type=ItemObject)

        # Pages that default to a grid (albums) can be shown as a list instead;
        # the choice lives in the main menu and is shared by every such page.
        self.switchable = layout == "grid"
        if self.switchable and win.cfg.get("album_view") == "list":
            self.layout = "list"
        scroller = self.scroller = Gtk.ScrolledWindow(vexpand=True)
        self._build_view()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        spinner.set_size_request(32, 32)
        self.empty = Adw.StatusPage(icon_name="folder-music-symbolic", title="Nothing here")
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(spinner, "loading")
        self.stack.add_named(scroller, "content")
        self.stack.add_named(self.empty, "empty")

        self.header = Adw.HeaderBar()
        self.header.pack_start(win.make_sidebar_toggle())
        if self.switchable:
            # The choice is made in the main menu (View); pick it up when shown.
            self.connect("showing", lambda _p: self.apply_layout(win.cfg.get("album_view")))
        if actions_item is not None:
            add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add all to queue")
            add.connect("clicked", lambda _b: win.play_item(actions_item, "add"))
            play = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Play all")
            play.connect("clicked", lambda _b: win.play_item(actions_item, "load"))
            self.header.pack_end(add)
            self.header.pack_end(play)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self.header)
        toolbar.set_content(self.stack)
        self.set_child(toolbar)
        if autoload:
            self.reload()

    # ------------------------------------------------------------ grid / list

    def _build_view(self):
        """(Re)create the grid or list widget on top of the same item store."""
        grid = self.layout == "grid"
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_cell if grid else self._setup_row)
        factory.connect("bind", self._bind_cell if grid else self._bind_row)
        model = Gtk.NoSelection(model=self.store)
        if grid:
            self.view = Gtk.GridView(model=model, factory=factory, min_columns=2, max_columns=14)
            self.view.add_css_class("album-grid")
        else:
            self.view = Gtk.ListView(model=model, factory=factory)
            self.view.add_css_class("browse-list")
        self.view.set_single_click_activate(True)
        self.view.connect("activate", self._activated)
        self.scroller.set_child(self.view)

    def apply_layout(self, layout):
        """Switch an album page between grid and list. Other pages ignore it."""
        if not self.switchable or layout not in ("grid", "list") or layout == self.layout:
            return
        self.layout = layout
        self._build_view()

    # ---------------------------------------------------------------- loading

    def reload(self):
        self._gen += 1
        self.store.remove_all()
        self.stack.set_visible_child_name("loading")
        self._load(0)

    def _load(self, start):
        gen = self._gen
        loader = self.loader
        run_async(lambda: loader(start, self.CHUNK),
                  lambda res: self._loaded(gen, res),
                  lambda exc: self._failed(gen, exc))

    def _loaded(self, gen, result):
        if gen != self._gen:
            return
        items, total = result
        if items:
            self.store.splice(self.store.get_n_items(), 0, [ItemObject(i) for i in items])
        have = self.store.get_n_items()
        self.show_state(have)
        if items and have < min(total, self.limit):
            self._load(have)

    def show_state(self, have):
        self.stack.set_visible_child_name("content" if have else "empty")

    def _failed(self, gen, exc):
        if gen != self._gen:
            return
        self.empty.set_title("Could not load this page")
        self.empty.set_description(str(exc))
        self.stack.set_visible_child_name("empty")

    def _activated(self, _view, position):
        obj = self.store.get_item(position)
        if obj is not None:
            self.win.open_item(obj.item)

    # ------------------------------------------------------------------- rows

    def _setup_row(self, _factory, li):
        box = Gtk.Box(spacing=12, margin_top=5, margin_bottom=5, margin_start=10, margin_end=8)
        cover = Cover(44, "cover-small")
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER,
                       hexpand=True, spacing=2)
        title = _label()
        sub = _label(("kora-dim", "kora-small"))
        text.append(title)
        text.append(sub)
        duration = _label(("kora-dim", "numeric"))
        add = _flat_button("list-add-symbolic", "Add to queue")
        play = _flat_button("media-playback-start-symbolic", "Play now")
        chevron = Gtk.Image(icon_name="go-next-symbolic")
        chevron.add_css_class("dim-label")
        add.connect("clicked", lambda _b: self.win.play_item(li.get_item().item, "add"))
        play.connect("clicked", lambda _b: self.win.play_item(li.get_item().item, "load"))
        for widget in (cover, text, duration, add, play, chevron):
            box.append(widget)
        box.parts = (cover, title, sub, duration, add, play, chevron)
        li.set_child(box)

    def _bind_row(self, _factory, li):
        item = li.get_item().item
        cover, title, sub, duration, add, play, chevron = li.get_child().parts
        title.set_label(item.title)
        year = item.extra.get("year") if item.kind == "album" else None
        detail = " · ".join(str(x) for x in (item.subtitle, year) if x)
        sub.set_label(detail)
        sub.set_visible(bool(detail))
        secs = item.extra.get("duration") if item.kind == "track" else 0
        duration.set_label(fmt_time(secs) if secs else "")
        add.set_visible(item.playable)
        play.set_visible(item.playable)
        chevron.set_visible(item.browsable)
        cover.show_art(self.win.art, item.art, icon_for(item))

    # ------------------------------------------------------------------ cells

    def _setup_cell(self, _factory, li):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                      margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        cover = Cover(168)
        play = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Play now",
                          halign=Gtk.Align.END, valign=Gtk.Align.END,
                          margin_end=8, margin_bottom=8)
        play.add_css_class("osd")
        play.add_css_class("circular")
        play.connect("clicked", lambda _b: self.win.play_item(li.get_item().item, "load"))
        cover.add_overlay(play)
        title = _label(("heading",), max_width_chars=18, margin_top=4)
        sub = _label(("kora-dim", "kora-small"), max_width_chars=18)
        box.append(cover)
        box.append(title)
        box.append(sub)
        box.parts = (cover, title, sub)
        li.set_child(box)

    def _bind_cell(self, _factory, li):
        item = li.get_item().item
        cover, title, sub = li.get_child().parts
        title.set_label(item.title)
        year = item.extra.get("year")
        sub.set_label(" · ".join(str(x) for x in (item.subtitle, year) if x))
        cover.show_art(self.win.art, item.art, icon_for(item))


class SearchPage(BrowsePage):
    def __init__(self, win):
        self.term = ""
        super().__init__(win, "Search", self._search, autoload=False)
        self.entry = Gtk.SearchEntry(placeholder_text="Artists, albums and songs", hexpand=True)
        self.entry.set_search_delay(300)
        self.entry.connect("search-changed", self._changed)
        clamp = Adw.Clamp(maximum_size=520, child=self.entry, hexpand=True)
        self.header.set_title_widget(clamp)
        self.empty.set_icon_name("system-search-symbolic")
        self.empty.set_title("Search your library")
        self.stack.set_visible_child_name("empty")
        self.connect("shown", lambda _p: self.entry.grab_focus())

    def _search(self, start, _count):
        if start or not self.term:
            return [], 0
        items = self.win.client.search(self.term)
        return items, len(items)

    def _changed(self, entry):
        self.term = entry.get_text().strip()
        self.empty.set_description("")
        if not self.term:
            self._gen += 1
            self.store.remove_all()
            self.empty.set_title("Search your library")
            self.stack.set_visible_child_name("empty")
            return
        self.empty.set_title("No results")
        self.reload()


class CollectionPage(Adw.NavigationPage):
    """An album or playlist: artwork header, actions and the track list."""

    def __init__(self, win, item, loader):
        super().__init__(title=item.title)
        self.win = win
        self.item = item
        self.tracks = []

        cover = Cover(200)
        big_art = item.art.replace("cover_300x300_o", "cover_500x500_o")
        cover.show_art(win.art, big_art, ICONS.get(item.kind, "media-optical-symbolic"))
        cover.set_valign(Gtk.Align.START)

        title = Gtk.Label(label=item.title, xalign=0, wrap=True)
        title.add_css_class("title-1")
        sub = Gtk.Label(label=item.subtitle, xalign=0, wrap=True, visible=bool(item.subtitle))
        sub.add_css_class("title-4")
        self.info = Gtk.Label(xalign=0)
        self.info.add_css_class("kora-dim")

        play = Gtk.Button(child=Adw.ButtonContent(icon_name="media-playback-start-symbolic",
                                                  label="Play"))
        play.add_css_class("suggested-action")
        play.add_css_class("pill")
        play.connect("clicked", lambda _b: win.play_item(item, "load"))
        nxt = Gtk.Button(label="Play next")
        nxt.add_css_class("pill")
        nxt.connect("clicked", lambda _b: win.play_item(item, "insert"))
        add = Gtk.Button(label="Add to queue")
        add.add_css_class("pill")
        add.connect("clicked", lambda _b: win.play_item(item, "add"))
        buttons = Gtk.Box(spacing=8, margin_top=12)
        for b in (play, nxt, add):
            buttons.append(b)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                       valign=Gtk.Align.CENTER, hexpand=True)
        for w in (title, sub, self.info, buttons):
            text.append(w)
        head = Gtk.Box(spacing=24)
        head.append(cover)
        head.append(text)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        self.listbox.connect("row-activated", self._row_activated)
        self.spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, margin_top=24)
        self.spinner.set_size_request(32, 32)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                       margin_top=24, margin_bottom=24, margin_start=18, margin_end=18)
        body.append(head)
        body.append(self.spinner)
        body.append(self.listbox)
        self.listbox.set_visible(False)
        scroller = Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=900, child=body), vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        header = Adw.HeaderBar()
        header.pack_start(win.make_sidebar_toggle())
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(scroller)
        self.set_child(toolbar)
        run_async(loader, self._loaded, self._failed)

    def _loaded(self, tracks):
        self.tracks = tracks
        self.spinner.set_visible(False)
        self.listbox.set_visible(True)
        total = sum(t.extra.get("duration") or 0 for t in tracks)
        bits = [self.item.extra.get("year"),
                f"{len(tracks)} song{'s' if len(tracks) != 1 else ''}",
                fmt_time(total) if total else None]
        self.info.set_label(" · ".join(str(b) for b in bits if b))
        is_album = self.item.kind == "album"
        for pos, track in enumerate(tracks):
            row = Adw.ActionRow(title=GLib.markup_escape_text(track.title), activatable=True)
            row.set_title_lines(1)
            artist = track.extra.get("artist", "")
            if artist and not (is_album and artist == self.item.subtitle):
                row.set_subtitle(GLib.markup_escape_text(artist))
                row.set_subtitle_lines(1)
            number = track.extra.get("tracknum") if is_album else pos + 1
            num = Gtk.Label(label=str(number or pos + 1), width_chars=3, xalign=1)
            num.add_css_class("kora-dim")
            num.add_css_class("numeric")
            row.add_prefix(num)
            secs = track.extra.get("duration")
            if secs:
                dur = Gtk.Label(label=fmt_time(secs))
                dur.add_css_class("kora-dim")
                dur.add_css_class("numeric")
                row.add_suffix(dur)
            add = _flat_button("list-add-symbolic", "Add to queue")
            add.connect("clicked", lambda _b, t=track: self.win.play_item(t, "add"))
            row.add_suffix(add)
            self.listbox.append(row)

    def _failed(self, exc):
        self.spinner.set_visible(False)
        self.info.set_label(str(exc))

    def _row_activated(self, _box, row):
        # Load the whole album/playlist and start at the chosen track.
        self.win.play_item(self.item, "load", index=row.get_index())
