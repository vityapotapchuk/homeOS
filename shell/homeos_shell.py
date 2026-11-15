#!/usr/bin/env python3
"""HomeOS shell - one maximised window with three views:

  home      the touch home screen (HTML UI served by sysapi.py)
  browser   WebKit browser with tabs and a touch toolbar
  terminal  a real terminal (VTE)

Run `homeos-shell --windowed --dev` inside any desktop session to try it.
"""

import argparse
import os
import signal
import sys
import threading

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
try:
    gi.require_version("WebKit2", "4.1")
except ValueError:
    gi.require_version("WebKit2", "4.0")

from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango, Vte, WebKit2  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sysapi  # noqa: E402

APP_ID = "homeos-shell"

TEXT = {"new_tab": "New tab", "address": "Search or enter address",
        "saved": "Downloaded: %s", "failed": "Download failed", "terminal": "Terminal"}

# Material Design 3 colour roles in Google's palette, same as ui/style.css
PALETTES = {
    "light": {"surface": "#ffffff", "bar": "#f0f4f9", "field": "#e9eef6", "on": "#1f1f1f", "on_var": "#444746",
              "primary": "#0b57d0", "tonal": "#c2e7ff", "tonal_press": "#a8d8f5", "inv": "#303030",
              "inv_on": "#f2f2f2", "state": "31, 31, 31", "on_primary": "#ffffff"},
    "dark": {"surface": "#131314", "bar": "#1e1f20", "field": "#282a2c", "on": "#e3e3e3", "on_var": "#c4c7c5",
             "primary": "#a8c7fa", "tonal": "#004a77", "tonal_press": "#0a5a8c", "inv": "#e3e3e3",
             "inv_on": "#303030", "state": "227, 227, 227", "on_primary": "#062e6f"},
}
THEME = "light"

CSS_TEMPLATE = """
.homeos-bar { background-color: %(bar)s; padding: 8px; }
.homeos-bar button {
  min-width: 48px; min-height: 48px; padding: 0; border-radius: 24px;
  background-image: none; background-color: transparent; border: none;
  box-shadow: none; color: %(on)s;
}
.homeos-bar button:hover { background-color: rgba(%(state)s, 0.08); }
.homeos-bar button:active { background-color: rgba(%(state)s, 0.12); }
.homeos-bar button:disabled { background-color: transparent; }
.homeos-bar button:disabled image { opacity: 0.38; }
.homeos-bar button.accent { background-color: %(tonal)s; }
.homeos-bar button.accent:active { background-color: %(tonal_press)s; }
.homeos-bar entry {
  min-height: 48px; border-radius: 24px; padding: 0 20px; font-size: 16px;
  background-image: none; background-color: %(field)s; color: %(on)s; border: none;
  box-shadow: none; caret-color: %(primary)s;
}
.homeos-bar entry:focus { box-shadow: inset 0 0 0 2px %(primary)s; }
.homeos-bar entry progress { background-color: %(primary)s; border: none; min-height: 3px; border-radius: 2px; }
.homeos-title { color: %(on)s; font-size: 22px; font-weight: normal; padding: 0 12px; }
.homeos-tabs header { background-color: %(bar)s; border: none; }
.homeos-tabs tab { min-height: 48px; padding: 0 4px 0 16px; color: %(on_var)s; background: none; border: none;
  font-size: 14px; font-weight: 500; }
.homeos-tabs tab:checked { color: %(primary)s; box-shadow: inset 0 -3px %(primary)s; }
.homeos-tabs tab button { min-width: 40px; min-height: 40px; padding: 0; border: none; border-radius: 20px;
  background: none; box-shadow: none; color: inherit; }
.homeos-toast { background-color: %(inv)s; color: %(inv_on)s; padding: 14px 16px; font-size: 14px; }
.homeos-keys button { border-radius: 8px; background-color: %(field)s; padding: 0 14px; font-size: 14px; font-weight: 500; }
.homeos-keys button:checked { background-color: %(primary)s; }
.homeos-keys button:checked label { color: %(on_primary)s; }
"""

PALETTE = ["#202124", "#f28b82", "#81c995", "#fdd663", "#8ab4f8", "#c58af9", "#78d9ec", "#e8eaed",
           "#9aa0a6", "#f6aea9", "#a8dab5", "#fde293", "#aecbfa", "#d7aefb", "#a1e4f2", "#ffffff"]


def rgba(spec):
    c = Gdk.RGBA()
    c.parse(spec)
    return c


ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")


ICON_IMAGES = []     # every toolbar image, so a theme change can repaint them


def set_icon(image, name, size=24):
    """Show one of our own icons (PNG, so no SVG loader or icon theme is needed)."""
    if not hasattr(image, "homeos_icon"):
        ICON_IMAGES.append(image)
    image.homeos_icon = (name, size)
    scale = max(1, image.get_scale_factor())
    path = os.path.join(ICON_DIR, THEME, name + ".png")
    try:
        if scale > 1:
            try:  # sharp icons on HiDPI screens; needs python3-gi-cairo
                pb = GdkPixbuf.Pixbuf.new_from_file_at_size(path, size * scale, size * scale)
                image.set_from_surface(Gdk.cairo_surface_create_from_pixbuf(pb, scale, None))
                return
            except TypeError:
                pass
        image.set_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_size(path, size, size))
    except GLib.Error:
        image.set_from_icon_name("image-missing", Gtk.IconSize.LARGE_TOOLBAR)


def icon_button(icon, tooltip=None, css=None, size=24):
    b = Gtk.Button()
    img = Gtk.Image()
    set_icon(img, icon, size)
    b.set_image(img)
    b.set_always_show_image(True)
    b.set_can_focus(False)
    if tooltip:
        b.set_tooltip_text(tooltip)
    if css:
        b.get_style_context().add_class(css)
    return b


ERROR_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light dark">
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#fff;
color:#1f1f1f;font-family:Roboto,"Noto Sans",system-ui,sans-serif}main{max-width:520px;padding:32px}
@media (prefers-color-scheme: dark){body{background:#131314;color:#e3e3e3}p{color:#c4c7c5 !important}}
h1{font-size:24px;line-height:32px;font-weight:400;margin:0 0 16px}p{color:#444746;line-height:20px;font-size:14px;margin:0 0 8px;
overflow-wrap:anywhere}</style><main><h1>%s</h1><p>%s</p><p>%s</p></main>"""


def html_escape(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class GtkHost:
    """Lets API threads ask the GTK main loop to do things."""

    def __init__(self, shell):
        self.shell = shell

    def apply_theme(self, theme):
        GLib.idle_add(self.shell.apply_theme, theme)

    def open_browser(self, url):
        GLib.idle_add(self.shell.open_browser, url)

    def open_terminal(self):
        GLib.idle_add(self.shell.show_view, "terminal")

    def quit_session(self):
        GLib.idle_add(self.shell.quit_session)


class Shell(Gtk.Window):
    def __init__(self, args):
        super().__init__(title="HomeOS")
        self.args = args
        self.exit_code = 0
        self.server = sysapi.Server(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui"),
            sysapi.default_config_dir(), host=GtkHost(self), dev=args.dev).start()

        self.set_default_size(*args.size)
        if not args.windowed:
            self.set_decorated(False)
            self.maximize()
        self.connect("destroy", lambda *_: Gtk.main_quit())
        self.connect("key-press-event", self.on_key)

        self.provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), self.provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.home_view = None
        self.apply_theme(self.server.settings.get()["theme"])

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(120)
        self.add(self.stack)

        self.build_home()
        self.build_browser()
        self.build_terminal()
        self.show_all()
        self.toast_revealer.set_reveal_child(False)
        self.show_view("home")

    # ------------------------------------------------------------ helpers
    def apply_theme(self, theme):
        """Light or dark Material colours for the bars, tabs and toolbar icons."""
        global THEME
        THEME = theme if theme in PALETTES else "light"
        self.provider.load_from_data((CSS_TEMPLATE % PALETTES[THEME]).encode())
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", THEME == "dark")
        for image in ICON_IMAGES:
            set_icon(image, *image.homeos_icon)
        if self.home_view is not None:
            self.home_view.set_background_color(rgba(PALETTES[THEME]["surface"]))
        return False

    def t(self, key):
        return TEXT[key]

    def show_view(self, name):
        if name == "terminal" and not self.term_running:
            self.spawn_shell()
        self.stack.set_visible_child_name(name)
        if name == "terminal":
            self.term.grab_focus()
        elif name == "home":
            self.home_view.grab_focus()
        return False

    def toggle_keyboard(self, *_):
        threading.Thread(target=self.server.system.toggle_keyboard, daemon=True).start()

    def quit_session(self):
        self.exit_code = 0
        self.destroy()
        return False

    def on_key(self, _w, ev):
        ctrl = ev.state & Gdk.ModifierType.CONTROL_MASK
        alt = ev.state & Gdk.ModifierType.MOD1_MASK
        key = Gdk.keyval_name(ev.keyval) or ""
        view = self.stack.get_visible_child_name()
        if alt and key == "Home":
            self.show_view("home")
            return True
        if view == "browser" and ctrl:
            if key in ("l", "L"):
                self.url_entry.grab_focus()
                return True
            if key in ("t", "T"):
                self.new_tab(None)
                return True
            if key in ("w", "W"):
                self.close_tab(self.current_view())
                return True
            if key in ("r", "R") and self.current_view():
                self.current_view().reload()
                return True
        return False

    # ------------------------------------------------------------ home
    def build_home(self):
        self.home_view = WebKit2.WebView()
        s = self.home_view.get_settings()
        s.set_enable_developer_extras(self.args.dev)
        self.home_view.set_background_color(rgba(PALETTES[THEME]["surface"]))
        self.home_view.connect("context-menu", lambda *_: True)
        self.home_view.connect("decide-policy", self.on_home_policy)
        self.home_view.load_uri(self.server.url)
        self.stack.add_named(self.home_view, "home")

    def on_home_policy(self, _view, decision, dtype):
        # The home screen never leaves the local UI; anything else goes to the browser.
        if dtype in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                     WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            uri = decision.get_navigation_action().get_request().get_uri() or ""
            if not uri.startswith(self.server.url) and uri != "about:blank":
                decision.ignore()
                self.open_browser(uri)
                return True
        return False

    # ------------------------------------------------------------ browser
    def build_browser(self):
        data = os.path.join(GLib.get_user_data_dir(), "homeos", "browser")
        cache = os.path.join(GLib.get_user_cache_dir(), "homeos", "browser")
        os.makedirs(data, exist_ok=True)
        os.makedirs(cache, exist_ok=True)
        mgr = WebKit2.WebsiteDataManager(base_data_directory=data, base_cache_directory=cache)
        self.web_ctx = WebKit2.WebContext.new_with_website_data_manager(mgr)
        mgr.get_cookie_manager().set_persistent_storage(
            os.path.join(data, "cookies.sqlite"), WebKit2.CookiePersistentStorage.SQLITE)
        self.web_ctx.connect("download-started", self.on_download)

        self.web_settings = WebKit2.Settings()
        self.web_settings.set_enable_smooth_scrolling(True)
        self.web_settings.set_enable_back_forward_navigation_gestures(True)
        self.web_settings.set_enable_developer_extras(self.args.dev)
        self.web_settings.set_javascript_can_open_windows_automatically(False)
        self.web_settings.set_media_playback_requires_user_gesture(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.browser_bar = Gtk.Box(spacing=6)
        self.browser_bar.get_style_context().add_class("homeos-bar")

        home = icon_button("home", css="accent")
        home.connect("clicked", lambda *_: self.show_view("home"))
        self.btn_back = icon_button("back")
        self.btn_back.connect("clicked", lambda *_: self.current_view() and self.current_view().go_back())
        self.btn_fwd = icon_button("forward")
        self.btn_fwd.connect("clicked", lambda *_: self.current_view() and self.current_view().go_forward())
        self.btn_reload = icon_button("reload")
        self.btn_reload.connect("clicked", self.on_reload)
        self.reload_shows_stop = False

        self.url_entry = Gtk.Entry()
        self.url_entry.set_placeholder_text(self.t("address"))
        self.url_entry.set_input_purpose(Gtk.InputPurpose.URL)
        self.url_entry.connect("activate", self.on_url_activate)
        self.url_entry.connect("focus-in-event", lambda e, _ev: GLib.idle_add(e.select_region, 0, -1) and False)

        newtab = icon_button("tab-new")
        newtab.connect("clicked", lambda *_: self.new_tab(None))
        kbd = icon_button("keyboard")
        kbd.connect("clicked", self.toggle_keyboard)

        for w in (home, self.btn_back, self.btn_fwd, self.btn_reload):
            self.browser_bar.pack_start(w, False, False, 0)
        self.browser_bar.pack_start(self.url_entry, True, True, 0)
        self.browser_bar.pack_start(newtab, False, False, 0)
        self.browser_bar.pack_start(kbd, False, False, 0)

        self.toast_label = Gtk.Label(xalign=0)
        self.toast_label.get_style_context().add_class("homeos-toast")
        self.toast_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.toast_revealer = Gtk.Revealer()
        self.toast_revealer.add(self.toast_label)
        self.toast_timer = 0

        self.tabs = Gtk.Notebook()
        self.tabs.set_scrollable(True)
        self.tabs.set_show_border(False)
        self.tabs.get_style_context().add_class("homeos-tabs")
        self.tabs.connect("switch-page", lambda _n, page, _i: self.sync_toolbar(page))

        box.pack_start(self.browser_bar, False, False, 0)
        box.pack_start(self.toast_revealer, False, False, 0)
        box.pack_start(self.tabs, True, True, 0)
        self.stack.add_named(box, "browser")

    def current_view(self):
        n = self.tabs.get_current_page()
        return self.tabs.get_nth_page(n) if n >= 0 else None

    def open_browser(self, url=None):
        cur = self.current_view()
        if url:
            if cur is not None and (cur.get_uri() or "about:blank") == "about:blank" and not cur.is_loading():
                cur.load_uri(url)
            else:
                self.new_tab(url)
        elif cur is None:
            self.new_tab(None)
        self.show_view("browser")
        return False

    def new_tab(self, url, related=None):
        if related is not None:
            view = WebKit2.WebView.new_with_related_view(related)
        else:
            view = WebKit2.WebView.new_with_context(self.web_ctx)
        view.set_settings(self.web_settings)
        view.connect("notify::title", self.on_tab_changed)
        view.connect("notify::uri", self.on_tab_changed)
        view.connect("notify::estimated-load-progress", self.on_tab_changed)
        view.connect("load-changed", lambda v, _e: self.on_tab_changed(v, None))
        view.connect("create", lambda v, _action: self.new_tab(None, related=v))
        view.connect("close", self.close_tab)
        view.connect("load-failed", self.on_load_failed)
        view.connect("load-failed-with-tls-errors", self.on_tls_failed)
        view.connect("enter-fullscreen", self.on_fullscreen, True)
        view.connect("leave-fullscreen", self.on_fullscreen, False)

        label = Gtk.Label(label=self.t("new_tab"))
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_width_chars(14)
        label.set_max_width_chars(14)
        label.set_xalign(0)
        close = icon_button("close", size=16)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.connect("clicked", lambda *_: self.close_tab(view))
        tab = Gtk.Box(spacing=4)
        tab.pack_start(label, True, True, 0)
        tab.pack_start(close, False, False, 0)
        tab.show_all()
        view.tab_label = label

        view.show()
        idx = self.tabs.append_page(view, tab)
        self.tabs.set_tab_reorderable(view, True)
        self.tabs.set_current_page(idx)
        if url:
            view.load_uri(url)
        elif related is None:
            view.load_uri("about:blank")
            self.url_entry.set_text("")
            # after the notebook has switched pages, or the page steals the focus
            GLib.idle_add(lambda: self.url_entry.grab_focus() and False)
        return view

    def close_tab(self, view, *_):
        if view is None:
            return
        idx = self.tabs.page_num(view)
        if idx >= 0:
            self.tabs.remove_page(idx)
            view.destroy()
        if self.tabs.get_n_pages() == 0:
            self.url_entry.set_text("")
            self.show_view("home")

    def on_tab_changed(self, view, _param):
        title = view.get_title() or view.get_uri() or self.t("new_tab")
        if title == "about:blank":
            title = self.t("new_tab")
        view.tab_label.set_text(title)
        if view is self.current_view():
            self.sync_toolbar(view)

    def sync_toolbar(self, view):
        uri = view.get_uri() or ""
        if not self.url_entry.is_focus():
            self.url_entry.set_text("" if uri == "about:blank" else uri)
        self.btn_back.set_sensitive(view.can_go_back())
        self.btn_fwd.set_sensitive(view.can_go_forward())
        loading = view.is_loading()
        self.url_entry.set_progress_fraction(view.get_estimated_load_progress() if loading else 0)
        if loading != self.reload_shows_stop:
            self.reload_shows_stop = loading
            set_icon(self.btn_reload.get_image(), "stop" if loading else "reload")

    def on_reload(self, *_):
        v = self.current_view()
        if v is None:
            return
        if v.is_loading():
            v.stop_loading()
        else:
            v.reload()

    def on_url_activate(self, entry):
        cfg = self.server.settings.get()
        url = sysapi.normalize_url(entry.get_text(), cfg["search"])
        if not url:
            return
        v = self.current_view() or self.new_tab(None)
        v.load_uri(url)
        v.grab_focus()

    def on_load_failed(self, view, _event, uri, error):
        if error.matches(WebKit2.network_error_quark(), WebKit2.NetworkError.CANCELLED):
            return False
        if error.domain in ("WebKitPolicyError", "WebKitPluginError", "WebKitDownloadError"):
            return False  # e.g. the load turned into a download
        view.load_alternate_html(ERROR_PAGE % (
            "This page could not be opened", html_escape(uri), html_escape(error.message)), uri, None)
        return True

    def on_tls_failed(self, view, uri, _cert, _errors):
        view.load_alternate_html(ERROR_PAGE % (
            "This connection is not secure", html_escape(uri),
            "The site's security certificate could not be verified, so the page was not loaded. "
            "Check that the date and time on this device are correct."), uri, None)
        return True

    def on_fullscreen(self, _view, entering):
        self.browser_bar.set_visible(not entering)
        self.tabs.set_show_tabs(not entering)
        if entering:
            self.fullscreen()
        else:
            self.unfullscreen()
        return False

    # ---- downloads
    def on_download(self, _ctx, download):
        download.connect("decide-destination", self.on_download_dest)
        download.connect("finished", lambda d: self.toast(
            self.t("saved") % os.path.basename(GLib.filename_from_uri(d.get_destination())[0])
            if d.get_destination() else self.t("failed")))
        download.connect("failed", lambda *_: self.toast(self.t("failed")))

    def on_download_dest(self, download, suggested):
        folder = (GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
                  or os.path.expanduser("~/Downloads"))
        os.makedirs(folder, exist_ok=True)
        name = os.path.basename(suggested or "") or "download"
        name = name.lstrip(".") or "download"
        stem, ext = os.path.splitext(name)
        path, n = os.path.join(folder, name), 1
        while os.path.exists(path):
            path = os.path.join(folder, "%s (%d)%s" % (stem, n, ext))
            n += 1
        download.set_destination(GLib.filename_to_uri(path, None))
        return True

    def toast(self, text):
        self.toast_label.set_text(text)
        self.toast_revealer.set_reveal_child(True)
        if self.toast_timer:
            GLib.source_remove(self.toast_timer)

        def hide():
            self.toast_revealer.set_reveal_child(False)
            self.toast_timer = 0
            return False
        self.toast_timer = GLib.timeout_add_seconds(5, hide)

    # ------------------------------------------------------------ terminal
    def build_terminal(self):
        self.term_running = False
        self.term_scale = 1.0
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=6)
        bar.get_style_context().add_class("homeos-bar")

        home = icon_button("home", css="accent")
        home.connect("clicked", lambda *_: self.show_view("home"))
        title = Gtk.Label(label=self.t("terminal"), xalign=0)
        title.get_style_context().add_class("homeos-title")
        smaller = icon_button("zoom-out")
        smaller.connect("clicked", lambda *_: self.zoom_terminal(-0.1))
        bigger = icon_button("zoom-in")
        bigger.connect("clicked", lambda *_: self.zoom_terminal(0.1))
        kbd = icon_button("keyboard")
        kbd.connect("clicked", self.toggle_keyboard)
        self.term_title = title

        bar.pack_start(home, False, False, 0)
        bar.pack_start(title, True, True, 0)
        for w in (smaller, bigger, kbd):
            bar.pack_start(w, False, False, 0)

        # key bar for touch: Esc, Tab, sticky Ctrl, arrows, copy and paste
        keys = Gtk.Box(spacing=8)
        keys.get_style_context().add_class("homeos-bar")
        keys.get_style_context().add_class("homeos-keys")

        def text_key(label, handler):
            b = Gtk.Button(label=label)
            b.set_can_focus(False)
            b.connect("clicked", handler)
            return b

        self.key_ctrl = Gtk.ToggleButton(label="Ctrl")
        self.key_ctrl.set_can_focus(False)
        left_keys = [text_key("Esc", lambda *_: self.send_bytes(b"\x1b")),
                     text_key("Tab", lambda *_: self.send_bytes(b"\t")), self.key_ctrl]
        arrows = []
        for icon, keyval in (("back", Gdk.KEY_Left), ("down", Gdk.KEY_Down), ("up", Gdk.KEY_Up), ("forward", Gdk.KEY_Right)):
            b = icon_button(icon)
            b.connect("clicked", lambda _b, k=keyval: self.send_key(k))
            arrows.append(b)
        copy = icon_button("copy")
        copy.connect("clicked", self.copy_terminal)
        paste = icon_button("paste")
        paste.connect("clicked", lambda *_: (self.term.paste_clipboard(), self.term.grab_focus()))
        for w in left_keys:
            keys.pack_start(w, False, False, 0)
        keys.pack_start(Gtk.Box(), True, True, 0)
        for w in arrows:
            keys.pack_start(w, False, False, 0)
        keys.pack_start(Gtk.Box(), True, True, 0)
        keys.pack_start(copy, False, False, 0)
        keys.pack_start(paste, False, False, 0)

        self.term = Vte.Terminal()
        self.term.set_font(Pango.FontDescription("Monospace 13"))
        self.term.set_scrollback_lines(10000)
        self.term.set_mouse_autohide(True)
        self.term.set_colors(rgba("#e3e3e3"), rgba("#131314"), [rgba(c) for c in PALETTE])
        self.term.set_color_cursor(rgba("#a8c7fa"))
        self.term.connect("child-exited", self.on_term_exit)
        self.term.connect("key-press-event", self.on_term_key)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.term)

        box.pack_start(bar, False, False, 0)
        box.pack_start(scroll, True, True, 0)
        box.pack_start(keys, False, False, 0)
        self.stack.add_named(box, "terminal")

    def send_bytes(self, data):
        self.term.feed_child(data)
        self.key_ctrl.set_active(False)
        self.term.grab_focus()

    def send_key(self, keyval):
        """Presses a real key on the terminal, so arrows follow the mode the
        running program asked for (shell history, editors, menus)."""
        self.key_ctrl.set_active(False)
        self.term.grab_focus()
        try:
            ev = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
            ev.window = self.term.get_window()
            ev.keyval = keyval
            ev.state = 0
            ev.send_event = True
            ev.time = Gtk.get_current_event_time()
            ev.set_device(Gdk.Display.get_default().get_default_seat().get_keyboard())
            self.term.event(ev)
        except (TypeError, AttributeError):
            codes = {Gdk.KEY_Up: b"A", Gdk.KEY_Down: b"B", Gdk.KEY_Right: b"C", Gdk.KEY_Left: b"D"}
            self.term.feed_child(b"\x1b[" + codes[keyval])

    def copy_terminal(self, *_):
        """Copies the selection, or the whole visible screen when nothing is selected."""
        if self.term.get_has_selection():
            self.term.copy_clipboard_format(Vte.Format.TEXT)
            self.term_note("Selection copied")
        else:
            try:
                text = self.term.get_text_format(Vte.Format.TEXT)
            except AttributeError:
                text = self.term.get_text(None, None)[0]
            clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clip.set_text((text or "").rstrip(), -1)
            clip.store()
            self.term_note("Screen copied")
        self.term.grab_focus()

    def term_note(self, text):
        self.term_title.set_text(self.t("terminal") + "  ·  " + text)
        GLib.timeout_add_seconds(2, lambda: self.term_title.set_text(self.t("terminal")) and False)

    def spawn_shell(self):
        shell = os.environ.get("SHELL") or "/bin/bash"
        if not os.path.exists(shell):
            shell = "/bin/sh"
        self.term_running = True
        self.term.spawn_async(
            Vte.PtyFlags.DEFAULT, os.path.expanduser("~"), [shell], None,
            GLib.SpawnFlags.DEFAULT, None, None, -1, None, None, None)

    def on_term_exit(self, _term, _status):
        self.term_running = False
        self.term.reset(True, True)
        if self.stack.get_visible_child_name() == "terminal":
            self.show_view("home")

    def on_term_key(self, _term, ev):
        if self.key_ctrl.get_active():
            # sticky Ctrl from the key bar applies to the next character typed
            code = Gdk.keyval_to_unicode(ev.keyval)
            if code:
                c = ord(chr(code).upper())
                self.key_ctrl.set_active(False)
                if 64 <= c <= 95:
                    self.term.feed_child(bytes([c - 64]))
                    return True
        mods = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
        if ev.state & mods == mods:
            key = (Gdk.keyval_name(ev.keyval) or "").lower()
            if key == "c":
                self.term.copy_clipboard_format(Vte.Format.TEXT)
                return True
            if key == "v":
                self.term.paste_clipboard()
                return True
        return False

    def zoom_terminal(self, delta):
        self.term_scale = max(0.6, min(2.5, self.term_scale + delta))
        self.term.set_font_scale(self.term_scale)


def main():
    ap = argparse.ArgumentParser(description="HomeOS shell")
    ap.add_argument("--windowed", action="store_true", help="normal window instead of maximised")
    ap.add_argument("--dev", action="store_true", help="web inspector on; reboot/poweroff disabled")
    ap.add_argument("--size", default="1280x720", help="window size in windowed mode, e.g. 800x480")
    args = ap.parse_args()
    try:
        args.size = tuple(int(x) for x in args.size.lower().split("x"))[:2]
        assert len(args.size) == 2
    except (ValueError, AssertionError):
        args.size = (1280, 720)

    GLib.set_prgname(APP_ID)
    GLib.set_application_name("HomeOS")
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    shell = Shell(args)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda *_: shell.destroy() or False)
    Gtk.main()
    sys.exit(shell.exit_code)


if __name__ == "__main__":
    main()
