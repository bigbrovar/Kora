"""Entry point.

    python3 -m kora                 normal start
    python3 -m kora --safe          without styling, adaptive layout and MPRIS
    python3 -m kora --without=css,breakpoints,mpris   leave out chosen parts
    python3 -m kora --diagnose      try each combination and print a verdict
"""

import faulthandler
import os
import subprocess
import sys
import traceback

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, __version__  # noqa: E402
from .config import Config  # noqa: E402

PARTS = ("css", "breakpoints", "mpris")
HERE = os.path.dirname(os.path.abspath(__file__))


def log(*parts):
    print("kora:", *parts, file=sys.stderr, flush=True)


def banner(skip):
    note = f" (without {', '.join(sorted(skip))})" if skip else ""
    log(f"{__version__}{note}",
        f"| GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}",
        f"| libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}",
        f"| PyGObject {gi.__version__}",
        f"| Python {sys.version.split()[0]}",
        f"| session {os.environ.get('XDG_SESSION_TYPE', '?')}"
        f"/{os.environ.get('XDG_CURRENT_DESKTOP', '?')}")


class KoraApp(Adw.Application):
    def __init__(self, skip=(), probe=False):
        flags = Gio.ApplicationFlags.DEFAULT_FLAGS
        if probe:
            flags |= Gio.ApplicationFlags.NON_UNIQUE   # do not hand off to a running Kora
        super().__init__(application_id=APP_ID, flags=flags)
        self.skip = set(skip)
        self.probe = probe
        self.cfg = Config()
        self.exit_code = 0
        self.frames = 0

    def do_startup(self):
        Adw.Application.do_startup(self)
        # Running from a source checkout: use the icons that ship beside the code.
        icons = os.path.join(os.path.dirname(HERE), "data", "icons")
        display = Gdk.Display.get_default()
        if os.path.isdir(icons) and display is not None:
            Gtk.IconTheme.get_for_display(display).add_search_path(icons)
        Gtk.Window.set_default_icon_name(APP_ID)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_a: self._quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", lambda *_a: self._about())
        self.add_action(about)

    def _about(self):
        from .window import show_about
        show_about(self.get_active_window())

    def _quit(self):
        window = self.get_active_window()
        if window is not None:
            window.close()
        self.quit()

    def do_activate(self):
        window = self.get_active_window()
        if window is None:
            try:
                from .window import KoraWindow
                window = KoraWindow(self)
            except Exception:
                # Without this the half-built window keeps the process alive
                # with nothing on screen.
                log("could not build the main window:")
                traceback.print_exc()
                if not self.probe:
                    log("try:  python3 -m kora --diagnose")
                for half_built in self.get_windows():
                    half_built.destroy()
                self.exit_code = 1
                self.quit()
                return
            GLib.timeout_add_seconds(5, self._watchdog, window)
            window.present()
            clock = window.get_frame_clock()
            if clock is not None:
                clock.connect("after-paint", self._painted)
        else:
            window.present()
        log("window presented")

    def _painted(self, _clock):
        self.frames += 1

    def _watchdog(self, window):
        """Five seconds in: is the window really on screen?"""
        shown = window.get_mapped() and self.frames > 0
        if self.probe:
            log(f"probe mapped={window.get_mapped()} frames={self.frames} "
                f"size={window.get_width()}x{window.get_height()}")
            self.quit()
            return False
        if shown:
            return False
        log("the window has not appeared after 5 seconds. Details:")
        try:
            surface = window.get_surface()
            renderer = window.get_renderer()
            log("  visible", window.get_visible(), "| realized", window.get_realized(),
                "| mapped", window.get_mapped(), "| frames drawn", self.frames,
                "| size", window.get_width(), "x", window.get_height(),
                "| default", window.get_default_size())
            log("  display", type(window.get_display()).__name__,
                "| surface", type(surface).__name__ if surface else None,
                "| renderer", type(renderer).__name__ if renderer else None)
        except Exception:
            traceback.print_exc()
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        log("try:  python3 -m kora --diagnose")
        return False


def diagnose():
    """Start Kora once per combination of optional parts and report which
    ones put a window on screen. Each run lasts about five seconds."""
    banner(())
    combos = [()] + [(p,) for p in PARTS] + [PARTS]
    results = []
    for skip in combos:
        label = "everything on" if not skip else "without " + ", ".join(skip)
        log(f"testing: {label} ...")
        cmd = [sys.executable, "-m", "kora", "--probe"]
        if skip:
            cmd.append("--without=" + ",".join(skip))
        env = dict(os.environ)   # so the child finds this copy, installed or not
        env["PYTHONPATH"] = os.pathsep.join(
            x for x in (os.path.dirname(HERE), env.get("PYTHONPATH", "")) if x)
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=25, env=env)
            text = out.stderr + out.stdout
        except subprocess.TimeoutExpired as exc:
            text = "TIMEOUT (froze) " + str((exc.stderr or b"")[-600:])
        probe = next((ln for ln in text.splitlines() if "probe mapped=" in ln), "")
        ok = "mapped=True" in probe and "frames=0 " not in probe
        notes = [ln for ln in text.splitlines()
                 if ln.startswith(("kora:", "Traceback", "  File")) or "Error" in ln
                 or "WARNING" in ln or "CRITICAL" in ln]
        results.append((label, ok, probe.replace("kora: probe ", ""), notes))
    print("\n===== Kora diagnosis: paste everything below =====")
    for label, ok, probe, notes in results:
        print(f"[{'WINDOW OK' if ok else 'NO WINDOW'}] {label:32} {probe or 'no probe line'}")
    print("----- details")
    for label, ok, _probe, notes in results:
        if not ok or label == "everything on":
            print(f"## {label}")
            for line in notes[-25:]:
                print("   ", line)
    return 0


def main():
    faulthandler.enable()
    argv = list(sys.argv)
    if "--diagnose" in argv:
        return diagnose()
    skip = set()
    if "--safe" in argv or os.environ.get("KORA_SAFE") == "1":
        skip.update(PARTS)
    for arg in argv:
        if arg.startswith("--without="):
            skip.update(x.strip() for x in arg.split("=", 1)[1].split(",") if x.strip())
    skip.update(x for x in os.environ.get("KORA_WITHOUT", "").split(",") if x)
    probe = "--probe" in argv
    argv = [a for a in argv if a not in ("--safe", "--probe") and not a.startswith("--without=")]
    banner(skip)
    app = KoraApp(skip=skip, probe=probe)
    status = app.run(argv)
    return status or app.exit_code


if __name__ == "__main__":
    sys.exit(main())
