from gi.repository import Gtk, Adw, Pango
from ui.utils import AsyncImage, parse_item_metadata, bind_weak_signal

CARD_SIZE_DEFAULT = 150
CARD_SIZE_COMPACT = 130


class MediaCardWidget(Gtk.Button):
    def __init__(
        self,
        item,
        player=None,
        title_lines=1,
        subtitle_text=None,
        custom_icon=None,
        fallback_icon="media-playlist-audio-symbolic",
        on_clicked=None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.item_data = item
        self.player = player
        self._compact = False
        
        self.target_size = CARD_SIZE_DEFAULT

        self.add_css_class("activatable")
        self.add_css_class("artist-horizontal-item")
        self.add_css_class("flat")
        self.set_hexpand(False)
        self.set_halign(Gtk.Align.START)

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.set_child(self.main_box)

        self.wrapper = Gtk.Box()
        self.wrapper.set_overflow(Gtk.Overflow.HIDDEN)
        self.wrapper.add_css_class("card-cover")
        self.wrapper.set_halign(Gtk.Align.CENTER)
        self.wrapper.set_valign(Gtk.Align.CENTER)

        if custom_icon:
            self.icon_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.icon_box.add_css_class("card-download-icon")
            icon = Gtk.Image.new_from_icon_name(custom_icon)
            icon.set_hexpand(True)
            icon.set_vexpand(True)
            self.icon_box.append(icon)
            self.wrapper.append(self.icon_box)
            self._cover_img = None
        else:
            thumbnails = item.get("thumbnails", [])
            thumb_url = thumbnails[-1].get("url") if thumbnails else None
            self._cover_img = AsyncImage(url=thumb_url, size=CARD_SIZE_DEFAULT, player=player)
            self._cover_img.add_css_class("card-cover-img")
            self._cover_img.video_id = (
                item.get("videoId") or item.get("playlistId") or item.get("browseId")
            )
            if not thumb_url and fallback_icon:
                self._cover_img.set_from_icon_name(fallback_icon)
            self.wrapper.append(self._cover_img)

        self.main_box.append(self.wrapper)

        title = item.get("title", "")
        self.title_label = Gtk.Label(label=title)
        self.title_label.set_halign(Gtk.Align.START)
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_label.set_wrap(True)
        self.title_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.title_label.set_lines(title_lines)
        self.title_label.set_justify(Gtk.Justification.LEFT)
        self.title_label.set_tooltip_text(title)

        self.title_clamp = Adw.Clamp()
        self.title_clamp.set_maximum_size(CARD_SIZE_DEFAULT)
        self.title_clamp.set_tightening_threshold(CARD_SIZE_DEFAULT)
        self.title_clamp.set_child(self.title_label)
        self.main_box.append(self.title_clamp)

        meta = parse_item_metadata(item)
        final_subtitle = subtitle_text if subtitle_text is not None else self._resolve_subtitle(item, meta)

        subtitle_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        subtitle_box.set_halign(Gtk.Align.START)
        subtitle_box.set_hexpand(True)

        if meta.get("is_explicit"):
            explicit_lbl = Gtk.Label(label="E")
            explicit_lbl.add_css_class("explicit-badge")
            explicit_lbl.set_valign(Gtk.Align.CENTER)
            subtitle_box.append(explicit_lbl)

        if final_subtitle:
            self.subtitle_label = Gtk.Label(label=final_subtitle)
            self.subtitle_label.add_css_class("caption")
            self.subtitle_label.add_css_class("dim-label")
            self.subtitle_label.set_ellipsize(Pango.EllipsizeMode.END)
            self.subtitle_label.set_lines(1)
            self.subtitle_label.set_hexpand(True)
            self.subtitle_label.set_halign(Gtk.Align.START)
            subtitle_box.append(self.subtitle_label)

        self.sub_clamp = None
        if final_subtitle or meta.get("is_explicit"):
            self.sub_clamp = Adw.Clamp()
            self.sub_clamp.set_maximum_size(CARD_SIZE_DEFAULT)
            self.sub_clamp.set_tightening_threshold(CARD_SIZE_DEFAULT)
            self.sub_clamp.set_child(subtitle_box)
            self.main_box.append(self.sub_clamp)

        if player and item.get("videoId"):
            self._attach_playing_state(item["videoId"])

        if on_clicked:
            self.connect("clicked", lambda btn: on_clicked(btn, self.item_data))

        self.connect("map", self._on_map)

    def set_compact_mode(self, compact: bool):
        self._compact = bool(compact)
        target_size = CARD_SIZE_COMPACT if compact else CARD_SIZE_DEFAULT

        if compact:
            self.add_css_class("compact")
        else:
            self.remove_css_class("compact")

        self.title_clamp.set_maximum_size(target_size)
        self.title_clamp.set_tightening_threshold(target_size)

        if self.sub_clamp:
            self.sub_clamp.set_maximum_size(target_size)
            self.sub_clamp.set_tightening_threshold(target_size)

        if self._cover_img and hasattr(self._cover_img, "set_compact"):
            self._cover_img.set_compact(compact)

    def set_compact(self, compact: bool):
        self.set_compact_mode(compact)

    def _is_ancestor_compact(self):
        widget = self.get_parent()
        while widget:
            if hasattr(widget, "has_css_class") and widget.has_css_class("compact"):
                return True
            widget = widget.get_parent()
        return False

    def _on_map(self, _widget):
        root = self.get_root()
        is_compact = (
            bool(getattr(root, "_is_compact", False))
            or self._is_ancestor_compact()
        )
        self.set_compact_mode(is_compact)

    def _resolve_subtitle(self, item, meta):
        parts = []
        if meta.get("year"):
            parts.append(str(meta["year"]))
        if meta.get("type") and meta.get("type").lower() not in [p.lower() for p in parts]:
            parts.append(meta.get("type"))

        if parts:
            return " • ".join(parts)
        if item.get("artists"):
            artists = item.get("artists")
            if isinstance(artists, list):
                return ", ".join([a.get("name", "") for a in artists if isinstance(a, dict)])
            return str(artists)
        return item.get("subtitle") or item.get("description") or ""

    def _attach_playing_state(self, video_id):
        def update_state(*_):
            current_id = getattr(self.player, "current_video_id", None)
            if current_id and current_id == video_id:
                self.add_css_class("playing")
                self.remove_css_class("flat")
            else:
                self.remove_css_class("playing")
                self.add_css_class("flat")

        update_state()
        bind_weak_signal(self.player, "metadata-changed", self, update_state)
        bind_weak_signal(self.player, "state-changed", self, update_state)
