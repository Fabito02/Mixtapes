from gi.repository import Gtk, GLib
from ui.util_classes import ScrolledWindow

class HorizontalScrollBox(Gtk.Overlay):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Scrolled Window
        self.scrolled = ScrolledWindow()
        self.scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self.scrolled.set_hexpand(True)
        self.scrolled.set_min_content_height(-1)
        
        # We need the adjustment to know if we can scroll
        self.hadjustment = self.scrolled.get_hadjustment()
        self.hadjustment.connect("value-changed", self._on_scroll_changed)
        self.hadjustment.connect("changed", self._on_scroll_changed)

        self.set_child(self.scrolled)

        # Left Button
        self.left_btn = Gtk.Button(icon_name="go-previous-symbolic")
        self.left_btn.add_css_class("circular")
        self.left_btn.add_css_class("osd") # Gives it a translucent look
        self.left_btn.set_valign(Gtk.Align.CENTER)
        self.left_btn.set_halign(Gtk.Align.START)
        self.left_btn.set_margin_start(8)
        self.left_btn.connect("clicked", self._scroll_left)
        self.left_btn.set_visible(False)
        self.add_overlay(self.left_btn)

        # Right Button
        self.right_btn = Gtk.Button(icon_name="go-next-symbolic")
        self.right_btn.add_css_class("circular")
        self.right_btn.add_css_class("osd")
        self.right_btn.set_valign(Gtk.Align.CENTER)
        self.right_btn.set_halign(Gtk.Align.END)
        self.right_btn.set_margin_end(8)
        self.right_btn.connect("clicked", self._scroll_right)
        self.right_btn.set_visible(False)
        self.add_overlay(self.right_btn)

        # Event controller for hover
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", self._on_enter)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

        self._is_hovered = False
        self._animating = False
        self._target_value = 0.0

    def set_content(self, child):
        self.scrolled.set_child(child)
        # Delay initial check
        GLib.idle_add(self._update_buttons)

    def _scroll_left(self, btn):
        value = self.hadjustment.get_value()
        page_size = self.hadjustment.get_page_size()
        # Scroll left by roughly one page/viewport size
        new_value = max(self.hadjustment.get_lower(), value - page_size * 0.8)
        self._animate_scroll(new_value)

    def _scroll_right(self, btn):
        value = self.hadjustment.get_value()
        page_size = self.hadjustment.get_page_size()
        upper = self.hadjustment.get_upper()
        # Scroll right by roughly one page/viewport size
        new_value = min(upper - page_size, value + page_size * 0.8)
        self._animate_scroll(new_value)

    def _animate_scroll(self, target_value):
        self._target_value = target_value
        if not self._animating:
            self._animating = True
            GLib.timeout_add(16, self._on_animate_step)

    def _on_animate_step(self):
        current = self.hadjustment.get_value()
        diff = self._target_value - current
        
        # Snap if close enough
        if abs(diff) < 2.0:
            self.hadjustment.set_value(self._target_value)
            self._animating = False
            return False
            
        # Interpolate
        step = diff * 0.15 # 15% closer every frame
        self.hadjustment.set_value(current + step)
        return True

    def _on_scroll_changed(self, adj):
        self._update_buttons()

    def _on_enter(self, controller, x, y):
        self._is_hovered = True
        self._update_buttons()

    def _on_leave(self, controller):
        self._is_hovered = False
        self._update_buttons()

    def _update_buttons(self):
        if not self._is_hovered:
            self.left_btn.set_visible(False)
            self.right_btn.set_visible(False)
            return

        value = self.hadjustment.get_value()
        lower = self.hadjustment.get_lower()
        upper = self.hadjustment.get_upper()
        page_size = self.hadjustment.get_page_size()

        # If we can scroll left
        can_scroll_left = value > lower + 1
        # If we can scroll right
        can_scroll_right = value + page_size < upper - 1

        self.left_btn.set_visible(can_scroll_left)
        self.right_btn.set_visible(can_scroll_right)
