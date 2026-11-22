"""Add a flight from a boarding pass: laptop camera, PDF / screenshot (file, drag and drop) or Ctrl+V."""
import os
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from . import barcode, bcbp, geo, stats
from .i18n import _


def _gst():
    try:
        gi.require_version("Gst", "1.0")
        gi.require_version("GstApp", "1.0")
        from gi.repository import Gst, GstApp  # noqa: F401  (GstApp adds AppSink.try_pull_sample)
        Gst.init(None)
        if Gst.ElementFactory.find("gtk4paintablesink") is None:
            return None
        return Gst
    except (ValueError, ImportError):
        return None


class Camera:
    """v4l2 camera -> preview paintable + grey frames read by ZXing in a worker thread."""

    def __init__(self, on_text, on_error):
        self.Gst = _gst()
        self.on_text, self.on_error = on_text, on_error
        self.pipeline = None
        self.paintable = None
        self._stop = threading.Event()
        self._thread = None

    @property
    def available(self):
        return self.Gst is not None

    def start(self):
        Gst = self.Gst
        if Gst is None:
            return False
        if self.pipeline is not None:
            return True                     # already running
        # SKYTRACK_CAMERA_SRC: another GStreamer source (tests, a phone used as a webcam, …)
        src = os.environ.get("SKYTRACK_CAMERA_SRC") or (
            "v4l2src" if Gst.ElementFactory.find("v4l2src") else "autovideosrc")
        desc = (f"{src} ! videoconvert ! tee name=t "
                "t. ! queue leaky=downstream max-size-buffers=2 ! videoconvert ! gtk4paintablesink name=view "
                "t. ! queue leaky=downstream max-size-buffers=1 ! videorate ! video/x-raw,framerate=4/1 ! "
                "videoconvert ! video/x-raw,format=GRAY8 ! appsink name=frames drop=true max-buffers=1 sync=false")
        try:
            self.pipeline = Gst.parse_launch(desc)
        except GLib.Error as e:
            self.on_error(str(e))
            return False
        self.paintable = self.pipeline.get_by_name("view").get_property("paintable")
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.on_error(_('Nie udało się włączyć kamery.'))
            self.stop()
            return False
        self._stop.clear()
        sink = self.pipeline.get_by_name("frames")
        self._thread = threading.Thread(target=self._read, args=(sink,), daemon=True)
        self._thread.start()
        return True

    def _on_bus_error(self, _bus, msg):
        err, _dbg = msg.parse_error()
        GLib.idle_add(lambda: (self.on_error(err.message), False)[1])
        self.stop()

    def _read(self, sink):
        Gst = self.Gst
        while not self._stop.is_set():
            sample = sink.try_pull_sample(Gst.SECOND // 2)
            if sample is None:
                continue
            s = sample.get_caps().get_structure(0)
            w, h = s.get_value("width"), s.get_value("height")
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if not ok:
                continue
            try:
                data = bytes(info.data)
            finally:
                buf.unmap(info)
            stride = len(data) // h if h else w
            texts = barcode.decode_pixels(data, w, h, barcode.FMT_LUM, stride, 1, harder=False)
            hits = [t for t in texts if bcbp.looks_like_bcbp(t)]   # ignore other codes in view
            if hits:
                GLib.idle_add(self._deliver, hits[0])
                return

    def _deliver(self, text):
        self.on_text(text, "camera")
        return False

    def stop(self):
        self._stop.set()
        if self.pipeline is not None:
            p, self.pipeline = self.pipeline, None
            p.set_state(self.Gst.State.NULL)
            p.get_bus().remove_signal_watch()
        self.paintable = None


class ScanDialog(Adw.Dialog):
    """Emits "scanned" with the list of Trip objects read from the pass (not saved yet)."""

    __gsignals__ = {"scanned": (GObject.SignalFlags.RUN_FIRST, None, (object,))}

    def __init__(self, airports, trips, airlines=None):
        super().__init__(title=_('Dodaj z karty pokładowej'), content_width=720, content_height=640)
        self.airports, self.trips, self.airlines = airports, trips, airlines or {}
        self.result = []
        self.camera = Camera(self._on_text, self._on_camera_error)

        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label=_('Anuluj'))
        cancel.connect("clicked", lambda *_: self.close())
        self.add_btn = Gtk.Button(label=_('Dodaj lot'), sensitive=False)
        self.add_btn.add_css_class("suggested-action")
        self.add_btn.connect("clicked", self._on_add)
        hb.pack_start(cancel)
        hb.pack_end(self.add_btn)
        tv.add_top_bar(hb)
        self.toasts = Adw.ToastOverlay()
        tv.set_content(self.toasts)
        self.set_child(tv)

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18,
                        margin_top=12, margin_bottom=18, margin_start=18, margin_end=18, homogeneous=True)
        self.toasts.set_child(outer)

        # left: source ---------------------------------------------------------------------------
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.append(left)
        mode = Gtk.Box(halign=Gtk.Align.START)
        mode.add_css_class("linked")
        self.btn_cam = Gtk.ToggleButton(child=Adw.ButtonContent(icon_name="camera-web-symbolic", label=_('Kamera')))
        self.btn_file = Gtk.ToggleButton(child=Adw.ButtonContent(icon_name="document-open-symbolic",
                                                                 label=_('PDF / obraz')), group=self.btn_cam)
        mode.append(self.btn_cam)
        mode.append(self.btn_file)
        left.append(mode)

        self.view_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        frame = Gtk.Frame(child=self.view_stack, height_request=300)
        left.append(frame)
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER)
        cam_overlay = Gtk.Overlay(child=self.picture)
        self.cam_hint = Gtk.Label(label=_('Pokaż kod z karty (Aztec, PDF417 lub QR) do kamery'),
                                  valign=Gtk.Align.END, margin_bottom=10, wrap=True, justify=Gtk.Justification.CENTER)
        self.cam_hint.add_css_class("osd")
        self.cam_hint.add_css_class("caption")
        cam_overlay.add_overlay(self.cam_hint)
        self.view_stack.add_named(cam_overlay, "camera")
        self.cam_status = Adw.StatusPage(icon_name="camera-disabled-symbolic", title=_('Kamera niedostępna'),
                                         description=_('Użyj PDF lub zrzutu ekranu z karty.'))
        self.cam_status.add_css_class("compact")
        self.view_stack.add_named(self.cam_status, "nocamera")

        drop = Adw.StatusPage(icon_name="document-send-symbolic", title=_('Upuść tu kartę pokładową'),
                              description=_('PDF z maila, zrzut ekranu z aplikacji linii lub zdjęcie. Możesz też wkleić obraz (Ctrl+V).'))
        drop.add_css_class("compact")
        pick = Gtk.Button(label=_('Wybierz plik…'), halign=Gtk.Align.CENTER)
        pick.add_css_class("pill")
        pick.connect("clicked", self._on_pick)
        drop.set_child(pick)
        self.view_stack.add_named(drop, "file")
        done = Adw.StatusPage(icon_name="object-select-symbolic", title=_('Odczytano kartę'))
        done.add_css_class("compact")
        done.add_css_class("success")
        again = Gtk.Button(label=_('Skanuj kolejną'), halign=Gtk.Align.CENTER)
        again.add_css_class("pill")
        again.connect("clicked", lambda *_a: self._on_mode())
        done.set_child(again)
        self.view_stack.add_named(done, "done")

        self.spinner = Gtk.Spinner(spinning=True, visible=False, halign=Gtk.Align.CENTER)
        left.append(self.spinner)

        # right: what was read -------------------------------------------------------------------
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.append(right)
        self.result_stack = Gtk.Stack(vexpand=True)
        right.append(self.result_stack)
        wait = Adw.StatusPage(icon_name="airplane-mode-symbolic", title=_('Czekam na kod'),
                              description=_('SkyTrack odczyta trasę, numer lotu, datę, miejsce i numer rezerwacji. Godziny dopiszesz przy zapisie.'))
        wait.add_css_class("compact")
        self.result_stack.add_named(wait, "wait")
        self.result_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.result_stack.add_named(Gtk.ScrolledWindow(child=self.result_box, hscrollbar_policy=Gtk.PolicyType.NEVER),
                                    "result")

        # inputs: drag and drop, Ctrl+V ----------------------------------------------------------
        target = Gtk.DropTarget.new(GObject.TYPE_NONE, Gdk.DragAction.COPY)
        target.set_gtypes([Gdk.FileList, Gdk.Texture, GObject.TYPE_STRING])
        target.connect("drop", self._on_drop)
        self.add_controller(target)
        keys = Gtk.ShortcutController(scope=Gtk.ShortcutScope.MANAGED)
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control>v"),
                                       action=Gtk.CallbackAction.new(lambda *_a: (self._paste(), True)[1])))
        self.add_controller(keys)

        self.btn_cam.connect("toggled", self._on_mode)
        self.connect("closed", lambda *_: self.camera.stop())
        if self.camera.available:
            self.btn_cam.set_active(True)
        else:
            self.btn_cam.set_sensitive(False)
            self.btn_file.set_active(True)
        self._on_mode()

    # sources --------------------------------------------------------------------------------------
    def _on_mode(self, *_a):
        if self.btn_cam.get_active():
            if self.camera.start():
                self.picture.set_paintable(self.camera.paintable)
                self.view_stack.set_visible_child_name("camera")
            else:
                self.view_stack.set_visible_child_name("nocamera")
        else:
            self.camera.stop()
            self.picture.set_paintable(None)
            self.view_stack.set_visible_child_name("file")

    def _on_camera_error(self, message):
        self.cam_status.set_description(message)
        self.view_stack.set_visible_child_name("nocamera")

    def _on_pick(self, *_a):
        dlg = Gtk.FileDialog(title=_('Karta pokładowa'))
        filters = Gio.ListStore.new(Gtk.FileFilter)
        f = Gtk.FileFilter()
        f.set_name(_('PDF lub obraz'))
        for pat in ("*.pdf", "*.PDF", "*.png", "*.PNG", "*.jpg", "*.JPG", "*.jpeg", "*.webp", "*.heic", "*.pkpass"):
            f.add_pattern(pat)
        filters.append(f)
        dlg.set_filters(filters)
        dlg.open(self.get_root(), None, self._on_picked)

    def _on_picked(self, dlg, res):
        try:
            gfile = dlg.open_finish(res)
        except GLib.Error:
            return
        if gfile and gfile.get_path():
            self._read_file(gfile.get_path())

    def _on_drop(self, _t, value, _x, _y):
        if isinstance(value, Gdk.FileList):
            files = value.get_files()
            if files and files[0].get_path():
                self._read_file(files[0].get_path())
                return True
        elif isinstance(value, Gdk.Texture):
            self._read_texture(value)
            return True
        elif isinstance(value, str):
            self._on_text(value.strip(), "text")
            return True
        return False

    def _paste(self):
        cb = self.get_clipboard()
        formats = cb.get_formats()
        if formats.contain_gtype(Gdk.Texture):
            cb.read_texture_async(None, self._on_paste_texture)
        elif formats.contain_gtype(Gdk.FileList):
            cb.read_value_async(Gdk.FileList, GLib.PRIORITY_DEFAULT, None, self._on_paste_files)
        else:
            cb.read_text_async(None, self._on_paste_text)

    def _on_paste_texture(self, cb, res):
        try:
            tex = cb.read_texture_finish(res)
        except GLib.Error:
            return
        if tex:
            self._read_texture(tex)

    def _on_paste_files(self, cb, res):
        try:
            fl = cb.read_value_finish(res)
        except GLib.Error:
            return
        files = fl.get_files() if fl else []
        if files and files[0].get_path():
            self._read_file(files[0].get_path())

    def _on_paste_text(self, cb, res):
        try:
            text = cb.read_text_finish(res)
        except GLib.Error:
            return
        if text:
            self._on_text(text.strip(), "text")

    def _busy(self, on):
        self.spinner.set_visible(on)

    def _read_file(self, path):
        self.btn_file.set_active(True)
        if path.lower().endswith(".pdf") and not barcode.pdf_available():
            self._toast(_('Do odczytu PDF potrzebny jest pakiet poppler-utils.'))
            return
        self._busy(True)

        def work():
            try:
                texts = barcode.decode_file(path)
            except Exception:           # unreadable / not an image
                texts = None
            GLib.idle_add(self._after_decode, texts)
        threading.Thread(target=work, daemon=True).start()

    def _read_texture(self, tex):
        self.btn_file.set_active(True)
        self._after_decode(barcode.decode_texture(tex))

    def _after_decode(self, texts):
        self._busy(False)
        if texts is None:
            self._toast(_('Nie udało się otworzyć tego pliku.'))
            return False
        hits = [t for t in texts if bcbp.looks_like_bcbp(t)]
        if hits:
            self._on_text(hits[0], "file")
        elif texts:
            self._toast(_('Znaleziono kod, ale to nie jest karta pokładowa.'))
        else:
            self._toast(_('Nie znaleziono kodu. Spróbuj wyraźniejszego zrzutu lub PDF z maila.'))
        return False

    # result ---------------------------------------------------------------------------------------
    def _on_text(self, text, source):
        try:
            bp = bcbp.parse(text)
        except bcbp.BCBPError:
            self._toast(_('To nie wygląda na kartę pokładową.'))
            return
        self.camera.stop()
        self.picture.set_paintable(None)
        self.view_stack.set_visible_child_name("done")
        self.result = bcbp.to_trips(bp, self.airlines)
        self._show(bp)

    def _show(self, bp):
        box = self.result_box
        while (c := box.get_first_child()) is not None:
            box.remove(c)
        unknown = []
        for leg, trip in zip(bp.legs, self.result):
            a, b = self.airports.get(trip.origin), self.airports.get(trip.dest)
            if not (a and b):
                unknown.append(trip.origin if not a else trip.dest)
            head = Gtk.Box(spacing=8)
            for code in (trip.origin, None, trip.dest):
                if code is None:
                    head.append(Gtk.Image(icon_name="airplane-mode-symbolic"))
                else:
                    lbl = Gtk.Label(label=code)
                    lbl.add_css_class("title-1")
                    head.append(lbl)
            box.append(head)
            if a and b:
                sub = Gtk.Label(xalign=0, label=f"{a.short} → {b.short} · {geo.fmt_km(geo.distance_km(a.lat, a.lon, b.lat, b.lon))}")
                sub.add_css_class("dim-label")
                box.append(sub)
            grp = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            grp.add_css_class("boxed-list")
            rows = [(_('Lot'), f"{trip.flight_no}" + (f" · {trip.airline}" if trip.airline else "")),
                    (_('Data'), stats.nice_date(trip.dep_date, True) if trip.dep_date else "—"),
                    (_('Miejsce'), trip.seat or "—"),
                    (_('Rezerwacja'), leg.pnr or "—")]
            for title, val in rows:
                r = Adw.ActionRow(title=title, subtitle=val)
                r.add_css_class("property")
                grp.append(r)
            box.append(grp)
            match = bcbp.find_match(self.trips, trip)
            note = Gtk.Label(xalign=0, wrap=True)
            note.add_css_class("caption")
            if match is not None:
                note.set_label(_('Ten lot jest już w dzienniku: uzupełnię miejsce, numer lotu i rezerwację.'))
                note.add_css_class("success")
            else:
                note.set_label(_('Nowy lot: przy zapisie dopiszesz godziny (karta ich nie zawiera).'))
                note.add_css_class("dim-label")
            box.append(note)
        if bp.passenger:
            who = Gtk.Label(xalign=0, label=_('Pasażer: {0}').format(bp.passenger))
            who.add_css_class("dim-label")
            box.append(who)
        self.result_stack.set_visible_child_name("result")
        ok = not unknown and all(t.dep_date for t in self.result)
        self.add_btn.set_sensitive(ok)
        if unknown:
            self._toast(_('Nieznane lotnisko: {0}').format(", ".join(unknown)))
        n = len(self.result)
        self.add_btn.set_label(_('Dodaj lot') if n == 1 else _('Dodaj loty ({0})').format(n))

    def _on_add(self, *_a):
        self.camera.stop()
        trips = self.result
        self.close()
        self.emit("scanned", trips)

    def _toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))
