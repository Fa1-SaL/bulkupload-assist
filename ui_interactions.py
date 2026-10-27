"""Small, widget-scoped Tk interactions; all work runs on Tk's event loop.

Scrollbar commands may keep calling tree.xview/tree.yview directly. Wheel
motion notices an externally changed view and yields to that direct movement.
"""
from __future__ import annotations

import math
import time
import tkinter as tk


UI_FONT_FAMILY = "Calibri"
_TK_ERRORS = (tk.TclError, RuntimeError)


def _exists(widget):
    try:
        return bool(widget.winfo_exists())
    except _TK_ERRORS:
        return False


def touchpad_deltas(widget, event):
    """Pixel (dx, dy) of a Tk 9 <TouchpadScroll> event, or (0, 0).

    Trackpads report these instead of <MouseWheel> on Tk 9. Content moves by
    the negated deltas, matching Tk's own bindings."""
    try:
        dx, dy = widget.tk.splitlist(widget.tk.call('tk::PreciseScrollDeltas', event.delta))
        return float(dx), float(dy)
    except (*_TK_ERRORS, ValueError, TypeError):
        return 0.0, 0.0


class SmoothTreeScroll:
    """Ease wheel input for one Treeview, without changing its selection.

    ``enabled`` returns whether this handler should own wheel events. When it
    returns False, native Tk bindings handle them. ``reduced_motion`` keeps
    normalized high-resolution input but applies it immediately. Call cancel()
    before a deliberate programmatic view reset, and destroy() when detaching.
    No global/class bindings or scrollbar commands are changed.
    """
    _FRAME_MS = 15
    _FRAMES = 8

    def __init__(self, tree, enabled=lambda: True, reduced_motion=lambda: False):
        self.tree = tree
        self.enabled = enabled
        self.reduced_motion = reduced_motion
        self._job = None
        self._destroyed = False
        self._bindings = []
        self._pending = {'x': 0.0, 'y': 0.0}
        self._fraction = {'x': 0.0, 'y': 0.0}
        self._expected = {}
        self._frames_left = 0
        try:
            self._system = str(tree.tk.call('tk', 'windowingsystem'))
        except _TK_ERRORS:
            self._system = ''
        self._touch = {'x': 0.0, 'y': 0.0}
        self._touch_job = None
        for sequence in ('<MouseWheel>', '<Shift-MouseWheel>', '<Button-4>',
                         '<Button-5>', '<Shift-Button-4>', '<Shift-Button-5>'):
            self._bindings.append((sequence, tree.bind(sequence, self.handle_wheel, add='+')))
        # Tk 9's own Treeview binding acts on only every fifth trackpad event and
        # moves in coarse units, which reads as lag. Handle every event in pixels.
        self._bindings.append(('<TouchpadScroll>', tree.bind('<TouchpadScroll>', self.handle_touchpad, add='+')))
        self._bindings.append(('<Destroy>', tree.bind('<Destroy>', self._on_destroy, add='+')))

    def _view(self, axis):
        return getattr(self.tree, axis + 'view')

    def _at_edge(self, axis, amount):
        first, last = self._view(axis)()
        return (amount < 0 and first <= 1e-9) or (amount > 0 and last >= 1 - 1e-9)

    def handle_wheel(self, event, axis=None):
        if self._destroyed or not _exists(self.tree):
            return None
        if not self.enabled():
            self.cancel()
            return None
        axis = axis or ('x' if getattr(event, 'state', 0) & 0x0001 else 'y')
        button = getattr(event, 'num', None)
        if button in (4, 5):
            amount = -3.0 if button == 4 else 3.0
        else:
            try:
                delta = float(getattr(event, 'delta', 0))
            except (ValueError, TypeError):
                return 'break'
            if not math.isfinite(delta):
                return 'break'
            # Windows reports 120 per notch, including partial high-resolution
            # notches. Aqua and X11 trackpads report small, possibly fractional
            # deltas. Accept standard 120-unit mouse notches on every platform.
            scale = 3.0 / 120.0 if self._system == 'win32' or abs(delta) >= 120 else 1.0
            amount = -delta * scale
        if not amount:
            return 'break'
        try:
            if self._at_edge(axis, amount):
                self._pending[axis] = self._fraction[axis] = 0.0
                return 'break'
            previous = self._pending[axis] + self._fraction[axis]
            if previous * amount < 0:
                self._pending[axis] = self._fraction[axis] = 0.0
            if self.reduced_motion():
                # A preference change during an existing animation must stop it.
                fractions = self._fraction.copy()
                self.cancel()
                self._fraction = fractions
                self._apply(axis, amount)
                return 'break'
            self._pending[axis] += amount
            self._expected[axis] = self._view(axis)()
            self._frames_left = self._FRAMES
            if self._job is None:
                self._job = self.tree.after(self._FRAME_MS, self._tick)
        except _TK_ERRORS:
            self.cancel()
        return 'break'

    def handle_touchpad(self, event):
        """Trackpads already supply inertia, so their pixels apply 1:1 with no easing.
        Events are coalesced to at most one view change per idle cycle."""
        if self._destroyed or not _exists(self.tree):
            return None
        if not self.enabled():
            return None
        dx, dy = touchpad_deltas(self.tree, event)
        if not dx and not dy:
            return 'break'
        if self._job is not None:
            self.cancel()  # Fingers take over from any eased wheel motion.
        self._touch['x'] -= dx
        self._touch['y'] -= dy
        if self._touch_job is None:
            self._touch_job = self.tree.after_idle(self._flush_touch)
        return 'break'

    def _row_height(self):
        try:
            style = self.tree.cget('style') or 'Treeview'
            return max(1.0, float(self.tree.tk.call('ttk::style', 'lookup', style, '-rowheight') or 20))
        except (*_TK_ERRORS, ValueError):
            return 20.0

    def _flush_touch(self):
        self._touch_job = None
        if self._destroyed or not _exists(self.tree):
            return
        try:
            if self._touch['x']:
                self._touch['x'] = self._move_x_pixels(self._touch['x'])
            if self._touch['y']:
                rows = self._touch['y'] / self._row_height()
                whole = math.trunc(rows)
                if whole and self._at_edge('y', whole):
                    self._touch['y'] = 0.0
                elif whole:
                    self.tree.yview('scroll', whole, 'units')
                    self._touch['y'] -= whole * self._row_height()
        except _TK_ERRORS:
            self._touch = {'x': 0.0, 'y': 0.0}

    def _move_x_pixels(self, pixels):
        """Move the horizontal view by pixels; return what could not be applied yet."""
        first, last = self._view('x')()
        visible = last - first
        if visible <= 0 or visible >= 1:
            return 0.0
        extent = max(1, self.tree.winfo_width()) / visible
        self._view('x')('moveto', first + pixels / extent)
        moved = (self._view('x')()[0] - first) * extent
        if self._at_edge('x', pixels):
            return 0.0
        return pixels - moved

    def _apply(self, axis, amount):
        total = self._fraction[axis] + amount
        if axis == 'x':
            # Treeview's horizontal "units" are coarse (~20 px). Moving by a
            # fraction of the content width gives pixel-sized animation steps.
            pixels = total * 20.0
            self._fraction['x'] = self._move_x_pixels(pixels) / 20.0
            if self._at_edge('x', pixels): self._fraction['x'] = self._pending['x'] = 0.0
            return
        units = math.trunc(round(total, 10))
        self._fraction[axis] = total - units
        if not units:
            return
        if self._at_edge(axis, units):
            self._pending[axis] = self._fraction[axis] = 0.0
            return
        self._view(axis)('scroll', units, 'units')
        if self._at_edge(axis, units):
            self._pending[axis] = self._fraction[axis] = 0.0

    def _tick(self):
        self._job = None
        if self._destroyed or not _exists(self.tree):
            self.cancel()
            return
        if not self.enabled():
            self.cancel()
            return
        try:
            # Direct scrollbar drags, keyboard movement, and view resets take
            # priority over an older queued wheel animation.
            for axis, expected in self._expected.items():
                actual = self._view(axis)()
                if any(abs(a - b) > 1e-8 for a, b in zip(actual, expected)):
                    self.cancel()
                    return
            immediate = self.reduced_motion()
            for axis in ('x', 'y'):
                pending = self._pending[axis]
                if not pending:
                    continue
                # Cubic ease-out: each remaining frame consumes its share of
                # the remaining distance, ending exactly after eight frames.
                ratio = 1.0 if immediate else 1 - ((self._frames_left - 1) / self._frames_left) ** 3
                amount = pending * ratio
                self._pending[axis] -= amount
                self._apply(axis, amount)
                self._expected[axis] = self._view(axis)()
            self._frames_left -= 1
            if not immediate and self._frames_left > 0 and any(self._pending.values()):
                self._job = self.tree.after(self._FRAME_MS, self._tick)
            else:
                self._expected.clear()
        except _TK_ERRORS:
            self.cancel()

    def cancel(self):
        """Discard queued momentum and fractions, leaving the view untouched."""
        if self._job is not None:
            try:
                self.tree.after_cancel(self._job)
            except _TK_ERRORS:
                pass
        self._job = None
        self._pending = {'x': 0.0, 'y': 0.0}
        self._fraction = {'x': 0.0, 'y': 0.0}
        self._expected.clear()
        self._frames_left = 0

    def _on_destroy(self, event):
        if event.widget is self.tree:
            self.destroy()

    def destroy(self):
        """Cancel motion and detach only the bindings owned by this instance."""
        if self._destroyed:
            return
        self.cancel()
        if self._touch_job is not None:
            try:
                self.tree.after_cancel(self._touch_job)
            except _TK_ERRORS:
                pass
            self._touch_job = None
        self._destroyed = True
        for sequence, function_id in self._bindings:
            try:
                self.tree.unbind(sequence, function_id)
            except _TK_ERRORS:
                pass
        self._bindings.clear()


# Shared motion tokens: exits run at about two thirds of entrances so closing
# feels responsive; entrances decelerate, exits accelerate.
MOTION_ENTER_MS = 220
MOTION_EXIT_MS = 150
_FRAME_MS = 15


def _ease(progress, ease):
    if ease == 'in':
        return progress * progress
    return 1 - (1 - progress) ** 3


class _Tween:
    """Time-based animation on Tk's event loop; frames may drop, the end time holds."""
    def __init__(self, widget, duration_ms, ease, step, on_done=None):
        self.widget = widget
        self._step = step
        self._ease = ease
        self._on_done = on_done
        self._duration = max(1, duration_ms) / 1000
        # The clock starts at the first frame: work done right after the call
        # (relayout, redraws) must not consume the animation before it is seen.
        self._started = None
        self._job = None
        self.active = True
        self._job = widget.after(_FRAME_MS, self._tick)

    def _tick(self):
        self._job = None
        if not self.active:
            return
        if not _exists(self.widget):
            self.cancel()
            return
        now = time.monotonic()
        if self._started is None:
            self._started = now - _FRAME_MS / 1000
        progress = min(1.0, (now - self._started) / self._duration)
        try:
            self._step(_ease(progress, self._ease))
        except _TK_ERRORS:
            self.cancel()
            return
        if progress < 1.0:
            self._job = self.widget.after(_FRAME_MS, self._tick)
            return
        self.active = False
        if self._on_done is not None:
            try:
                self._on_done()
            except _TK_ERRORS:
                pass

    def cancel(self):
        """Stop where it is; the completion callback does not run."""
        self.active = False
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except _TK_ERRORS:
                pass
            self._job = None

    __call__ = cancel


def is_animating(widget):
    tween = getattr(widget, '_ui_height_tween', None)
    return tween is not None and tween.active


def animate_height(widget, target, duration_ms=MOTION_ENTER_MS, ease='out', on_done=None):
    """Tween a non-propagating frame's height. A newer call replaces an older one,
    whose on_done is then skipped. duration_ms <= 0 applies the height at once."""
    previous = getattr(widget, '_ui_height_tween', None)
    if previous is not None:
        previous.cancel()
    widget._ui_height_tween = None
    target = max(0, int(target))
    try:
        start = int(float(widget.cget('height')))
    except (*_TK_ERRORS, ValueError):
        start = target
    if duration_ms <= 0 or start == target:
        widget.configure(height=target)
        if on_done is not None:
            on_done()
        return None
    tween = _Tween(widget, duration_ms, ease,
                   lambda eased: widget.configure(height=round(start + (target - start) * eased)), on_done)
    widget._ui_height_tween = tween
    return tween


class _WindowFade:
    """Fade a window's opacity between two values. Cancelling always leaves an
    existing window fully opaque; a closing fade withdraws it before that."""
    def __init__(self, win, reduce_motion, duration_ms, start=0.0, end=1.0, ease='out', on_done=None):
        self.win = win
        self._tween = None
        self._binding = None
        self._on_done = on_done
        if not _exists(win):
            return
        instant = reduce_motion or duration_ms <= 0
        try:
            if end > start:
                win.attributes('-alpha', end if instant else start)
                win.deiconify()
            if instant:
                self._finish()
                return
            self._binding = win.bind('<Destroy>', self._on_destroy, add='+')
            self._tween = _Tween(win, duration_ms, ease,
                lambda eased: win.attributes('-alpha', start + (end - start) * eased), self._finish)
        except _TK_ERRORS:
            # Some window managers do not implement alpha. Still show or hide the UI.
            self._finish()

    def _finish(self):
        done, self._on_done = self._on_done, None
        if done is not None:
            try:
                done()
            except _TK_ERRORS:
                pass
        self.cancel()

    def _on_destroy(self, event):
        if event.widget is self.win:
            self.cancel()

    def cancel(self):
        if self._tween is not None:
            self._tween.cancel()
            self._tween = None
        if self._binding is not None:
            try:
                self.win.unbind('<Destroy>', self._binding)
            except _TK_ERRORS:
                pass
            self._binding = None
        if _exists(self.win):
            try:
                self.win.attributes('-alpha', 1.0)
            except _TK_ERRORS:
                pass

    __call__ = cancel


def _replace_window_animation(win, animation_factory):
    previous = getattr(win, '_ui_open_animation', None)
    if previous is not None:
        previous.cancel()
    animation = animation_factory()
    win._ui_open_animation = animation
    return animation


def animate_window_open(win, reduce_motion=False, duration_ms=MOTION_ENTER_MS):
    """Show a window with a quick fade-in and no geometry changes.

    Returns a callable handle with cancel(). Repeated calls replace an earlier
    fade on the same window, including a close in progress. Unsupported opacity
    gracefully shows immediately.
    """
    return _replace_window_animation(win, lambda: _WindowFade(win, reduce_motion, duration_ms))


def animate_window_close(win, on_done, reduce_motion=False, duration_ms=MOTION_EXIT_MS):
    """Fade a window out, then call on_done (normally withdraw). Opening it
    again before the fade ends cancels the close."""
    return _replace_window_animation(win, lambda: _WindowFade(
        win, reduce_motion, duration_ms, start=1.0, end=0.0, ease='in', on_done=on_done))


def fade_through(win, change, reduce_motion=False):
    """Fade a window out, apply a jarring change (such as fullscreen), fade back in.
    Used where the platform has no transition of its own."""
    def reveal():
        try:
            change()
        finally:
            animate_window_open(win, reduce_motion)
    if reduce_motion:
        change()
        return None
    return _replace_window_animation(win, lambda: _WindowFade(
        win, False, MOTION_EXIT_MS, start=1.0, end=0.0, ease='in', on_done=reveal))


class Tooltip:
    """Hover hint for one widget. ``text`` may be a callable returning the
    current hint, or None to show nothing (e.g. while the control is usable)."""
    DELAY_MS = 450

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self._job = None
        self._tip = None
        for sequence, handler in (('<Enter>', self._schedule), ('<Leave>', self.hide),
                                  ('<ButtonPress>', self.hide)):
            widget.bind(sequence, handler, add='+')

    def _current_text(self):
        try:
            return self.text() if callable(self.text) else self.text
        except _TK_ERRORS:
            return None

    def _schedule(self, _event=None):
        self._cancel()
        if _exists(self.widget):
            self._job = self.widget.after(self.DELAY_MS, self.show)

    def _cancel(self):
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except _TK_ERRORS:
                pass
            self._job = None

    def show(self):
        self._job = None
        text = self._current_text()
        if not text or self._tip is not None or not _exists(self.widget):
            return
        try:
            tip = self._tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            tip.attributes('-topmost', True)
            tk.Label(tip, text=text, justify='left', wraplength=320, bg='#11141A', fg='#E6EAF0',
                     padx=10, pady=6, font=(UI_FONT_FAMILY, 12)).pack()
            x = self.widget.winfo_rootx()
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            tip.update_idletasks()
            # Keep the hint on screen near the right and bottom edges.
            x = min(x, self.widget.winfo_screenwidth() - tip.winfo_reqwidth() - 8)
            if y + tip.winfo_reqheight() > self.widget.winfo_screenheight() - 8:
                y = self.widget.winfo_rooty() - tip.winfo_reqheight() - 6
            tip.geometry(f'+{max(0, x)}+{max(0, y)}')
        except _TK_ERRORS:
            self.hide()

    def hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except _TK_ERRORS:
                pass
            self._tip = None
