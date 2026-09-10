import re
import threading

from gi.repository import Gtk, Adw, GObject, GLib, Pango, Gdk

from api.client import MusicClient
from ui.utils import (
    AsyncImage, AsyncPicture, parse_item_metadata, is_online, bind_weak_signal
)
from ui.context_menu import show_item_menu
from ui.widgets.scroll_box import HorizontalScrollBox
from ui.util_classes import ScrolledWindow


CARD_SIZE = 150
SPEED_TILE_COVER = 56
SONG_THUMB_SIZE = 56


# ─── Helpers: kind detection / labelling ────────────────────────────────────

_SONG_SECTION_KEYS = (
    "song", "track", "favorite", "listen again", "quick pick",
    "forgotten", "rediscover", "hidden gem", "recap", "your library",
    "from your library", "mix", "hits", "made for you",
)
_VIDEO_SECTION_KEYS = (
    "music video", "remix", "live performance", "performances",
    "video for you", "videos for you",
)


def _is_video_thumbnail(item):
    thumbs = item.get("thumbnails") or []
    for t in thumbs:
        url = (t.get("url") or "") if isinstance(t, dict) else ""
        if "/vi/" in url or "/vi_webp/" in url:
            return True
    return False


def _detect_kind(item, section_title=""):
    if not isinstance(item, dict):
        return None

    if item.get("videoId"):
        vtype = (item.get("videoType") or "").upper()
        if vtype:
            if vtype == "MUSIC_VIDEO_TYPE_ATV":
                return "song"
            if "PODCAST" in vtype or "EPISODE" in vtype:
                return None
            return "video"

        low = (section_title or "").lower()
        if any(k in low for k in _VIDEO_SECTION_KEYS):
            return "video"
        if any(k in low for k in _SONG_SECTION_KEYS):
            return "song"

        if _is_video_thumbnail(item):
            return "video"

        if (
            item.get("views")
            and not item.get("album")
            and not item.get("duration")
            and not item.get("year")
        ):
            return "video"
        return "song"

    if item.get("playlistId"):
        return "playlist"
    browse_id = item.get("browseId") or ""
    if browse_id.startswith("MPRE") or browse_id.startswith("OLAK"):
        return "album"
    if browse_id.startswith("UC") or browse_id.startswith("FEmusic_library_privately_owned"):
        return "artist"
    if item.get("subscribers") is not None:
        return "artist"
    if item.get("audioPlaylistId"):
        return "album"
    return None


def _kind_word(kind, item):
    if kind == "album":
        return item.get("type") or "Album"
    return {
        "song": "Song",
        "video": "Video",
        "playlist": "Playlist",
        "artist": "Artist",
    }.get(kind, "")


def _kind_icon(kind):
    return {
        "song": "audio-x-generic-symbolic",
        "video": "video-x-generic-symbolic",
        "album": "media-optical-symbolic",
        "playlist": "view-list-symbolic",
        "artist": "avatar-default-symbolic",
    }.get(kind)


def _artists_text(item):
    artists = item.get("artists") or []
    if isinstance(artists, list):
        names = [a.get("name", "") for a in artists if isinstance(a, dict)]
        text = ", ".join(n for n in names if n)
        if text:
            return text
    author = item.get("author")
    if isinstance(author, list):
        return ", ".join(
            a.get("name", "") if isinstance(a, dict) else str(a) for a in author
        )
    if isinstance(author, dict):
        return author.get("name", "")
    if isinstance(author, str):
        return author
    return ""


_UNIT_RE = re.compile(
    r"\b\d[\d.,]*\s*[KMB]?\s*"
    r"(songs?|episodes?|videos?|tracks?|views?|plays?|subscribers?|monthly listeners?|listeners?)\b",
    re.IGNORECASE,
)


def _playlist_detail(item):
    desc = (item.get("description") or "").strip()
    if desc:
        m = _UNIT_RE.search(desc)
        if m:
            return m.group(0)
        parts = [p.strip() for p in re.split(r"[•·]", desc) if p.strip()]
        if parts:
            return parts[-1]
    count = item.get("count") or ""
    if count:
        return f"{count} songs"
    author = _artists_text(item)
    return author or ""


def _video_detail(item):
    parts = []
    a = _artists_text(item)
    if a:
        parts.append(a)
    if item.get("views"):
        parts.append(item["views"])
    dur = _duration_str(item)
    if dur:
        parts.append(dur)
    return " · ".join(parts)


def _album_detail(item):
    parts = []
    a = _artists_text(item)
    if a:
        parts.append(a)
    meta = parse_item_metadata(item)
    if meta.get("year"):
        parts.append(meta["year"])
    return " · ".join(parts)


def _artist_detail(item):
    subs = item.get("subscribers") or ""
    if not subs:
        return ""
    return subs if any(c.isalpha() for c in subs) else f"{subs} subscribers"


def _song_detail(item):
    parts = []
    a = _artists_text(item)
    if a:
        parts.append(a)
    album = item.get("album")
    album_name = (
        album.get("name") if isinstance(album, dict)
        else (album if isinstance(album, str) else "")
    )
    if album_name and album_name != item.get("title"):
        parts.append(album_name)
    dur = _duration_str(item)
    if dur:
        parts.append(dur)
    return " · ".join(parts)


def _duration_str(item):
    dur = item.get("duration")
    if dur:
        return str(dur)
    secs = item.get("duration_seconds")
    if isinstance(secs, (int, float)) and secs > 0:
        secs = int(secs)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"
    return ""


def _detail_for(item, kind):
    if kind == "playlist":
        return _playlist_detail(item)
    if kind == "video":
        return _video_detail(item)
    if kind == "album":
        return _album_detail(item)
    if kind == "artist":
        return _artist_detail(item)
    return _song_detail(item)


# ─── Highlight Helper: toggle playing & flat ───────────────────────────────

def _attach_item_playing_state(widget, player, video_id, is_button=True):
    """Monitors player state and toggles .playing / .flat dynamically."""
    if not video_id:
        return

    def update_state(*args):
        current_id = getattr(player, "current_video_id", None)
        is_playing = bool(current_id and current_id == video_id)
        if is_playing:
            widget.add_css_class("playing")
            if is_button:
                widget.remove_css_class("flat")
        else:
            widget.remove_css_class("playing")
            if is_button:
                widget.add_css_class("flat")

    update_state()
    bind_weak_signal(player, "metadata-changed", widget, update_state)
    bind_weak_signal(player, "state-changed", widget, update_state)


# ─── Section ordering ───────────────────────────────────────────────────────

_PRIORITY = [
    ("library",      ["your library", "from your library"]),
    ("listen_again", ["listen again", "your favorites", "recent activity"]),
    ("discover",     ["daily discover", "discover mix", "discovery mix", "made for you", "recommended for today"]),
    ("forgotten",    ["forgotten favorites", "hidden gems", "rediscover"]),
]


def _classify_section(title):
    if not title:
        return None
    low = title.lower()
    for bucket, keys in _PRIORITY:
        if any(k in low for k in keys):
            return bucket
    return None


def _section_icon(title):
    if not title:
        return None
    low = title.lower()
    rules = [
        (["from your library", "your library"], "media-optical-symbolic"),
        (["forgotten", "rediscover", "hidden gem"], "starred-symbolic"),
        (["daily discover", "discovery mix", "discover mix"], "compass2-symbolic"),
        (["listen again"], "media-playback-start-symbolic"),
        (["mix"], "media-playlist-shuffle-symbolic"),
        (["new release", "new album", "new single"], "star-new-symbolic"),
        (["music video"], "video-x-generic-symbolic"),
        (["mood", "moment"], "emoji-objects-symbolic"),
        (["recap"], "media-playback-start-symbolic"),
        (["quick pick"], "media-playback-start-symbolic"),
    ]
    for keys, icon in rules:
        if any(k in low for k in keys):
            return icon
    return None


_PODCAST_SECTION_KEYS = (
    "shows for you", "long listen", "podcast", "episode",
)


def _is_podcast_section(title):
    if not title:
        return False
    low = title.lower()
    return any(k in low for k in _PODCAST_SECTION_KEYS)


# ─── HomePage ───────────────────────────────────────────────────────────────


class HomePage(Adw.Bin):
    def __init__(self, player, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.player = player
        self.client = MusicClient()
        self._compact = False
        self._loaded = False
        self._loading = False
        self._retry_count = 0

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)

        loading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        loading_box.set_valign(Gtk.Align.CENTER)
        loading_box.set_halign(Gtk.Align.CENTER)
        spinner = Adw.Spinner()
        spinner.set_size_request(32, 32)
        loading_box.append(spinner)
        loading_label = Gtk.Label(label="Loading…")
        loading_label.add_css_class("dim-label")
        loading_box.append(loading_label)
        self.stack.add_named(loading_box, "loading")

        feed_scroll = ScrolledWindow()
        feed_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        feed_clamp = Adw.Clamp()
        feed_clamp.set_maximum_size(1024)
        feed_clamp.set_tightening_threshold(600)
        self.feed_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28)
        self.feed_box.set_margin_top(24)
        self.feed_box.set_margin_bottom(24)
        self.feed_box.set_margin_start(12)
        self.feed_box.set_margin_end(12)
        feed_clamp.set_child(self.feed_box)
        feed_scroll.set_child(feed_clamp)
        self.stack.add_named(feed_scroll, "feed")

        self.status = Adw.StatusPage(
            icon_name="user-home-symbolic",
            title="Home",
            description="Your music feed will appear here.",
        )
        self.stack.add_named(self.status, "status")

        self.set_child(self.stack)
        self.stack.set_visible_child_name("loading")

        GLib.idle_add(self.load_home_data)

    # ─── Layout ────────────────────────────────────────────────────────────

    def set_compact_mode(self, compact):
        self._compact = compact
        if compact:
            self.add_css_class("compact")
            self.feed_box.set_spacing(20)
            self.feed_box.set_margin_start(6)
            self.feed_box.set_margin_end(6)
        else:
            self.remove_css_class("compact")
            self.feed_box.set_spacing(28)
            self.feed_box.set_margin_start(12)
            self.feed_box.set_margin_end(12)
        for strip in getattr(self, "_card_strips", []):
            strip.set_spacing(8 if compact else 16)
        self._propagate_compact(self.feed_box, compact)

    def _propagate_compact(self, widget, compact):
        if hasattr(widget, "has_css_class") and widget.has_css_class("home-section-header"):
            return
            
        if hasattr(widget, "set_compact"):
            try:
                widget.set_compact(compact)
            except Exception:
                pass
        child = widget.get_first_child() if hasattr(widget, "get_first_child") else None
        while child:
            self._propagate_compact(child, compact)
            child = child.get_next_sibling()

    # ─── Fetch ─────────────────────────────────────────────────────────────

    def load_home_data(self, force=False):
        if self._loading:
            return False
        if self._loaded and not force:
            return False
        self._loading = True
        if force:
            self._loaded = False
        self.stack.set_visible_child_name("loading")
        threading.Thread(target=self._fetch_home, daemon=True).start()
        return False

    def refresh(self):
        self.load_home_data(force=True)

    def _fetch_home(self):
        if not is_online():
            GObject.idle_add(self._apply_home, None, "offline")
            return
        try:
            data = self.client.get_home_full(limit=25)
            GObject.idle_add(self._apply_home, data, None)
        except Exception as e:
            print(f"[HOME] fetch failed: {e}")
            GObject.idle_add(self._apply_home, None, "error")

    def _apply_home(self, data, error_kind):
        self._loading = False
        if not data:
            if error_kind == "offline":
                self._show_status(
                    "network-offline-symbolic",
                    "You're offline",
                    "Home requires an internet connection.\nYour downloaded songs are still available.",
                )
                return
            if self._retry_count < 2:
                self._retry_count += 1
                GLib.timeout_add(
                    1500 * self._retry_count,
                    lambda: (self.load_home_data(force=True) or False) and False,
                )
                return
            self._show_status(
                "dialog-warning-symbolic",
                "Couldn't load Home",
                "Try refreshing in a moment.",
                show_retry=True,
            )
            return

        self._retry_count = 0
        self._loaded = True
        self._populate_feed(data)
        self.stack.set_visible_child_name("feed")

    # ─── Status / retry ────────────────────────────────────────────────────

    def _show_status(self, icon, title, description, show_retry=False):
        self.status.set_icon_name(icon)
        self.status.set_title(title)
        self.status.set_description(description)

        try:
            self.status.set_child(None)
        except Exception:
            pass

        if show_retry:
            retry = Gtk.Button(label="Retry")
            retry.add_css_class("pill")
            retry.add_css_class("suggested-action")
            retry.set_halign(Gtk.Align.CENTER)
            retry.connect(
                "clicked",
                lambda _b: (setattr(self, "_retry_count", 0), self.load_home_data(force=True)),
            )
            self.status.set_child(retry)

        self.stack.set_visible_child_name("status")

    # ─── Feed building ─────────────────────────────────────────────────────

    def _clear_feed(self):
        child = self.feed_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.feed_box.remove(child)
            child = nxt

    def _populate_feed(self, sections):
        self._clear_feed()
        if not sections:
            return

        sections = [
            s for s in sections
            if isinstance(s, dict)
            and s.get("contents")
            and not _is_podcast_section(s.get("title"))
        ]
        sections = [s for s in sections if any(_detect_kind(it, s.get("title") or "") for it in s["contents"])]

        speed_items = []
        speed_consumed = None
        for sec in sections:
            title = (sec.get("title") or "").lower()
            if "quick pick" in title:
                speed_items = sec["contents"]
                speed_consumed = sec
                break
        if speed_consumed is not None:
            sections = [s for s in sections if s is not speed_consumed]
        if not speed_items and sections:
            for sec in sections:
                if _classify_section(sec.get("title")) == "listen_again":
                    speed_items = sec["contents"]
                    break
            else:
                speed_items = sections[0]["contents"]

        if speed_items:
            self._add_speed_dial(speed_items)

        buckets = {b: None for b, _ in _PRIORITY}
        rest = []
        for sec in sections:
            bucket = _classify_section(sec.get("title"))
            if bucket and buckets[bucket] is None:
                buckets[bucket] = sec
            else:
                rest.append(sec)

        ordered = [buckets[b] for b, _ in _PRIORITY if buckets[b]] + rest

        for sec in ordered:
            title = sec.get("title") or ""
            contents = sec.get("contents") or []
            if not contents:
                continue
            bucket = _classify_section(title)
            strapline = sec.get("strapline_thumbnail")
            self._add_section(title, contents, bucket, strapline_url=strapline)

    # ─── Section heading ───────────────────────────────────────────────────

    def _make_section_header(self, title, bucket=None, strapline_url=None):
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        header.set_halign(Gtk.Align.START)
        header.add_css_class("home-section-header")

        if strapline_url:
            cover = AsyncImage(url=strapline_url, size=30, player=self.player)
            wrapper = Gtk.Box()
            wrapper.set_overflow(Gtk.Overflow.HIDDEN)
            wrapper.add_css_class("home-section-cover")
            wrapper.set_valign(Gtk.Align.CENTER)
            wrapper.append(cover)
            header.append(wrapper)
        else:
            icon_name = _section_icon(title)
            if icon_name:
                icon = Gtk.Image.new_from_icon_name(icon_name)
                icon.set_pixel_size(22)
                icon.add_css_class("home-section-icon")
                icon.set_valign(Gtk.Align.CENTER)
                header.append(icon)

        label = Gtk.Label(label=title or "")
        label.add_css_class("title-2")
        label.add_css_class("home-section-title")
        label.set_halign(Gtk.Align.START)
        label.set_valign(Gtk.Align.CENTER)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        header.append(label)

        return header

    # ─── Speed dial ────────────────────────────────────────────────────────

    def _add_speed_dial(self, items):
        section_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        section_box.add_css_class("home-speed-dial")
        self.feed_box.append(section_box)

        section_box.append(self._make_section_header("Quick picks"))

        scroll_box = HorizontalScrollBox()

        wrap = Adw.WrapBox(orientation=Gtk.Orientation.VERTICAL)
        wrap.set_line_homogeneous(True)
        wrap.set_line_spacing(8)
        wrap.set_child_spacing(8)

        wrap.set_size_request(-1, 250)
        wrap.set_valign(Gtk.Align.START)

        section_title = "Quick picks"
        playable_pool = [it for it in items if _detect_kind(it, section_title) in ("song", "video")]

        for item in items:
            kind = _detect_kind(item, section_title)
            if not kind:
                continue

            tile = self._build_speed_tile(
                item, kind, playable_pool,
                on_clicked=lambda btn, it=item, k=kind, pool=playable_pool: self._activate_item(it, k, pool)
            )
            wrap.append(tile)

        scroll_box.set_content(wrap)
        section_box.append(scroll_box)

    def _build_speed_tile(self, item, kind, playable_pool, on_clicked=None):
        tile = Gtk.Button()
        tile.add_css_class("home-speed-tile")
        tile.add_css_class("card")

        root = self.get_root()
        compact = bool(getattr(root, "_is_compact", False)) if root else self._compact
        tile_width = 200 if compact else 240
        tile.set_size_request(tile_width, -1)
        
        inner_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        tile.set_child(inner_box)

        thumb_url = (
            (item.get("thumbnails") or [{}])[-1].get("url")
            if item.get("thumbnails") else None
        )
        img = AsyncImage(url=thumb_url, size=SPEED_TILE_COVER, player=self.player)
        img.video_id = item.get("videoId") or item.get("playlistId") or item.get("browseId")

        wrapper = Gtk.Box()
        wrapper.set_overflow(Gtk.Overflow.HIDDEN)
        wrapper.add_css_class("home-speed-cover")
        wrapper.set_valign(Gtk.Align.CENTER)
        wrapper.append(img)
        inner_box.append(wrapper)

        text_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_col.set_valign(Gtk.Align.CENTER)
        text_col.set_hexpand(True)
        text_col.set_size_request(
            tile_width - SPEED_TILE_COVER - (22 if compact else 26), -1
        )

        title_label = Gtk.Label(label=item.get("title", "Unknown"))
        title_label.set_halign(Gtk.Align.FILL)
        title_label.set_xalign(0)
        title_label.set_ellipsize(Pango.EllipsizeMode.END)
        title_label.set_lines(2)
        title_label.set_wrap(True)
        title_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        title_label.set_max_width_chars(1)
        title_label.set_hexpand(True)
        title_label.add_css_class("home-speed-title")
        text_col.append(title_label)

        text_col.append(
            self._build_kind_subtitle(
                item, kind, dim=True, include_kind=True, include_kind_word=False,
                constrain_width=True
            )
        )
        inner_box.append(text_col)

        if on_clicked:
            tile.connect("clicked", lambda btn: on_clicked(tile))

        right = Gtk.GestureClick()
        right.set_button(3)
        right.connect("released", self._on_tile_right_click, tile, item, kind)
        tile.add_controller(right)

        lp = Gtk.GestureLongPress()
        lp.connect(
            "pressed",
            lambda g, x, y, t=tile, it=item, k=kind: self._on_tile_right_click(g, 1, x, y, t, it, k),
        )
        tile.add_controller(lp)

        _attach_item_playing_state(tile, self.player, item.get("videoId"), is_button=False)

        return tile

    def _on_speed_tile_activated(self, flowbox, child):
        self._activate_item(child.item_data, child.item_kind, getattr(child, "queue_pool", None))

    def _on_tile_right_click(self, gesture, n_press, x, y, anchor, item, kind):
        self._show_context_menu(anchor, x, y, item, kind)

    # ─── Section dispatch ──────────────────────────────────────────────────

    def _add_section(self, title, items, bucket=None, strapline_url=None):
        section_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.feed_box.append(section_box)

        section_box.append(
            self._make_section_header(title, bucket, strapline_url=strapline_url)
        )

        song_count = sum(1 for it in items if _detect_kind(it, title) == "song")
        if song_count >= max(3, int(len(items) * 0.66)):
            self._add_song_list(section_box, items, bucket, section_title=title)
        else:
            self._add_card_strip(section_box, items, bucket, section_title=title)

    # ─── Song list ─────────────────────────────────────────────────────────

    def _add_song_list(self, section_box, items, bucket=None, section_title=""):
        list_box = Gtk.ListBox()
        list_box.add_css_class("boxed-list")
        list_box.add_css_class("songs-list")
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        list_box.connect("row-activated", self._on_song_row_activated)

        playable = [it for it in items if _detect_kind(it, section_title) in ("song", "video")]

        for item in items:
            kind = _detect_kind(item, section_title)
            row = Gtk.ListBoxRow()
            row.item_data = item
            row.item_kind = kind
            row.queue_pool = playable
            row.set_activatable(True)

            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            box.add_css_class("song-row")
            row.set_child(box)

            _attach_item_playing_state(row, self.player, item.get("videoId"), is_button=False)

            thumb_url = (
                (item.get("thumbnails") or [{}])[-1].get("url")
                if item.get("thumbnails") else None
            )
            img = AsyncPicture(
                url=thumb_url,
                target_size=SONG_THUMB_SIZE,
                crop_to_square=True,
                player=self.player,
            )
            img.video_id = item.get("videoId")
            img.add_css_class("song-img")
            root = self.get_root()
            img.set_compact(getattr(root, "_is_compact", False) if root else False)
            if not thumb_url:
                img.set_from_icon_name("media-optical-symbolic")
            box.append(img)

            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            vbox.set_valign(Gtk.Align.CENTER)
            vbox.set_hexpand(True)

            title_lbl = Gtk.Label(label=item.get("title", "Unknown"))
            title_lbl.set_halign(Gtk.Align.START)
            title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            title_lbl.set_lines(1)

            title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            title_box.append(title_lbl)
            meta = parse_item_metadata(item)
            if meta.get("is_explicit"):
                explicit_badge = Gtk.Label(label="E")
                explicit_badge.add_css_class("explicit-badge")
                explicit_badge.set_valign(Gtk.Align.CENTER)
                title_box.append(explicit_badge)

            vbox.append(title_box)
            vbox.append(self._build_kind_subtitle(item, kind, dim=True))
            box.append(vbox)

            self._attach_context_menu(row, item, kind)
            list_box.append(row)

        section_box.append(list_box)

    # ─── Card strip ────────────────────────────────────────────────────────

    def _add_card_strip(self, section_box, items, bucket=None, section_title=""):
        scroll_box = HorizontalScrollBox()
        h_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        h_box.set_margin_bottom(16)
        if not hasattr(self, "_card_strips"):
            self._card_strips = []
        self._card_strips.append(h_box)

        for item in items:
            kind = _detect_kind(item, section_title)
            if not kind:
                continue
            card = self._build_card(item, kind, items, section_title=section_title)
            if card is not None:
                h_box.append(card)

        scroll_box.set_content(h_box)
        section_box.append(scroll_box)

    def _build_card(self, item, kind, siblings, section_title=""):
        card = Gtk.Button()
        card.add_css_class("activatable")
        card.add_css_class("artist-horizontal-item")
        card.add_css_class("flat")
        card.item_data = item
        card.item_kind = kind
        card.queue_pool = [
            it for it in siblings if _detect_kind(it, section_title) in ("song", "video")
        ]

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.set_child(box)

        thumb_url = (
            (item.get("thumbnails") or [{}])[-1].get("url")
            if item.get("thumbnails") else None
        )
        img = AsyncImage(url=thumb_url, size=CARD_SIZE, player=self.player)
        img.video_id = item.get("videoId") or item.get("playlistId") or item.get("browseId")

        wrapper = Gtk.Box()
        wrapper.set_overflow(Gtk.Overflow.HIDDEN)
        wrapper.add_css_class("card-cover")
        wrapper.set_halign(Gtk.Align.CENTER)
        wrapper.append(img)
        box.append(wrapper)

        title_lbl = Gtk.Label(label=item.get("title", ""))
        title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        title_lbl.set_wrap(True)
        title_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        title_lbl.set_lines(1)
        title_lbl.set_justify(Gtk.Justification.LEFT)
        title_lbl.set_halign(Gtk.Align.START)
        title_clamp = Adw.Clamp()
        title_clamp.set_child(title_lbl)
        box.append(title_clamp)

        sub_widget = self._build_kind_subtitle(item, kind, dim=True, include_kind=True)
        sub_clamp = Adw.Clamp()
        sub_clamp.set_child(sub_widget)
        box.append(sub_clamp)

        card.connect("clicked", self._on_card_clicked)

        right = Gtk.GestureClick()
        right.set_button(3)
        right.connect("released", self._on_card_right_click, card)
        card.add_controller(right)

        lp = Gtk.GestureLongPress()
        lp.connect(
            "pressed",
            lambda g, x, y, c=card: self._on_card_right_click(g, 1, x, y, c),
        )
        card.add_controller(lp)

        _attach_item_playing_state(card, self.player, item.get("videoId"), is_button=True)

        return card

    # ─── Subtitle row with kind icon + detail ──────────────────────────────

    def _build_kind_subtitle(
        self, item, kind, dim=True, include_kind=True, include_kind_word=None,
        constrain_width=False
    ):
        if include_kind_word is None:
            include_kind_word = include_kind
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        row.set_halign(Gtk.Align.FILL if constrain_width else Gtk.Align.START)

        icon_name = _kind_icon(kind) if include_kind else None
        if icon_name:
            icon = Gtk.Image.new_from_icon_name(icon_name)
            icon.set_pixel_size(12)
            icon.add_css_class("home-kind-icon")
            icon.set_valign(Gtk.Align.CENTER)
            if dim:
                icon.add_css_class("dim-label")
            row.append(icon)

        meta = parse_item_metadata(item)
        if meta.get("is_explicit") and not include_kind:
            explicit_lbl = Gtk.Label(label="E")
            explicit_lbl.add_css_class("explicit-badge")
            row.append(explicit_lbl)

        parts = []
        if include_kind_word:
            kw = _kind_word(kind, item)
            if kw:
                parts.append(kw)
        detail = _detail_for(item, kind)
        if detail:
            parts.append(detail)
        text = " · ".join(parts)

        if text:
            label = Gtk.Label(label=text)
            label.set_halign(Gtk.Align.START)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            label.set_lines(1)
            if constrain_width:
                label.set_max_width_chars(1)
                label.set_halign(Gtk.Align.FILL)
                label.set_xalign(0)
                label.set_hexpand(True)
            label.add_css_class("caption")
            if dim:
                label.add_css_class("dim-label")
            row.append(label)

        return row

    # ─── Activation ────────────────────────────────────────────────────────

    def _on_card_clicked(self, button):
        self._activate_item(button.item_data, button.item_kind, getattr(button, "queue_pool", None))

    def _on_song_row_activated(self, listbox, row):
        self._activate_item(row.item_data, row.item_kind, getattr(row, "queue_pool", None))

    def _activate_item(self, item, kind, queue_pool=None):
        if not item:
            return
        root = self.get_root()

        if kind in ("song", "video"):
            self._play_with_radio(item, queue_pool)
            return

        if kind == "playlist":
            pid = item.get("playlistId")
            if pid and root and hasattr(root, "open_playlist"):
                root.open_playlist(pid, self._initial_data(item))
            return

        if kind == "album":
            browse_id = item.get("browseId") or ""
            if browse_id.startswith("MPRE"):
                if root and hasattr(root, "open_playlist"):
                    root.open_playlist(browse_id, self._initial_data(item))
                return
            audio_pid = item.get("audioPlaylistId") or ""
            if audio_pid and root and hasattr(root, "open_playlist"):
                root.open_playlist(audio_pid, self._initial_data(item))
                return
            if browse_id and root and hasattr(root, "open_playlist"):
                root.open_playlist(browse_id, self._initial_data(item))
            return

        if kind == "artist":
            browse_id = item.get("browseId")
            if browse_id and root and hasattr(root, "open_artist"):
                root.open_artist(browse_id, item.get("title"))
            return

    def _play_with_radio(self, item, queue_pool):
        tracks = []
        start_index = 0

        if queue_pool:
            for sib in queue_pool:
                if not sib.get("videoId"):
                    continue
                thumbs = sib.get("thumbnails") or []
                thumb = thumbs[-1].get("url", "") if thumbs else ""
                qt = {
                    "videoId": sib["videoId"],
                    "title": sib.get("title", "Unknown"),
                    "artist": _artists_text(sib),
                    "thumb": thumb,
                }
                if isinstance(sib.get("artists"), list):
                    qt["artists"] = sib["artists"]
                if sib.get("album"):
                    qt["album"] = sib["album"]
                if sib.get("videoId") == item.get("videoId"):
                    start_index = len(tracks)
                tracks.append(qt)

        if not tracks:
            thumbs = item.get("thumbnails") or []
            thumb_url = thumbs[-1].get("url", "") if thumbs else ""
            self.player.load_video(
                item["videoId"],
                item.get("title", "Unknown"),
                _artists_text(item),
                thumb_url,
            )
            seed = item.get("videoId")
            if seed and hasattr(self.player, "play_then_radio"):
                self.player.play_then_radio(self.player.queue, 0, seed)
            return

        seed_vid = tracks[-1].get("videoId")
        if hasattr(self.player, "play_then_radio") and seed_vid:
            self.player.play_then_radio(tracks, start_index, seed_vid)
        else:
            self.player.set_queue(tracks, start_index)

    @staticmethod
    def _initial_data(item):
        thumbs = item.get("thumbnails") or []
        return {
            "title": item.get("title", ""),
            "thumb": thumbs[-1].get("url") if thumbs else None,
            "author": _artists_text(item),
        }

    # ─── Context menu ──────────────────────────────────────────────────────

    def _attach_context_menu(self, row, item, kind):
        right = Gtk.GestureClick()
        right.set_button(3)
        right.connect("released", self._on_row_right_click, row, item, kind)
        row.add_controller(right)
        lp = Gtk.GestureLongPress()
        lp.connect(
            "pressed",
            lambda g, x, y, r=row, it=item, k=kind: self._on_row_right_click(g, 1, x, y, r, it, k),
        )
        row.add_controller(lp)

    def _on_row_right_click(self, gesture, n_press, x, y, row, item, kind):
        self._show_context_menu(row, x, y, item, kind)

    def _on_card_right_click(self, gesture, n_press, x, y, card):
        self._show_context_menu(card, x, y, card.item_data, card.item_kind)

    def _show_context_menu(self, anchor, x, y, item, kind):
        show_item_menu(
            anchor,
            x,
            y,
            item,
            kind,
            player=self.player,
            client=self.client,
            prefix="row",
        )
        