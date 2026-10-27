"""Wheel and opening motion contracts, with a deterministic Tk scheduler."""
import types
import unittest
from unittest.mock import patch
import tkinter as tk
from tkinter import ttk

from ui_interactions import SmoothTreeScroll, animate_window_open


class FakeWidget:
    """Only the Tk boundary is replaced; motion and accumulation stay real."""
    def __init__(self, system='aqua'):
        self.tk = types.SimpleNamespace(call=lambda *args: system)
        self.bindings = {}
        self.jobs = {}
        self.serial = 0
        self.now_ms = 0
        self.alive = True
        self.position = {'x': 0.4, 'y': 0.4}
        self.selection = ('candidate-1',)
        self.alpha = 1.0
        self.visible = False
        self.scroll_history = []

    def bind(self, sequence, callback, add=None):
        self.serial += 1
        key = str(self.serial)
        self.bindings[(sequence, key)] = callback
        return key

    def unbind(self, sequence, key=None):
        self.bindings.pop((sequence, key), None)

    def after(self, delay, callback):
        self.serial += 1
        key = str(self.serial)
        self.jobs[key] = (delay, callback)
        return key

    def after_cancel(self, key):
        self.jobs.pop(key, None)

    def run_next(self):
        key = next(iter(self.jobs))
        delay, callback = self.jobs.pop(key)
        self.now_ms += delay
        callback()

    def drain(self):
        for _ in range(40):
            if not self.jobs:
                return
            self.run_next()
        raise AssertionError('Motion failed to settle within 40 frames')

    def wheel(self, delta=0, shift=False, button=None):
        sequence = '<MouseWheel>' if button is None else f'<Button-{button}>'
        if shift:
            sequence = '<Shift-' + sequence[1:]
        event = types.SimpleNamespace(delta=delta, state=1 if shift else 0,
                                      num=button, widget=self)
        callbacks = [callback for (seq, _), callback in self.bindings.items() if seq == sequence]
        return callbacks[-1](event)

    def winfo_exists(self):
        return self.alive

    def _view(self, axis, *args):
        if args:
            if args[0] == 'scroll':
                self.position[axis] = min(0.8, max(0.0, self.position[axis] + args[1] * 0.01))
                self.scroll_history.append((axis, self.position[axis]))
            elif args[0] == 'moveto':
                self.position[axis] = min(0.8, max(0.0, float(args[1])))
                self.scroll_history.append((axis, self.position[axis]))
        return self.position[axis], self.position[axis] + 0.2

    def xview(self, *args):
        return self._view('x', *args)

    def yview(self, *args):
        return self._view('y', *args)

    def winfo_width(self):
        return 400

    def attributes(self, name, value=None):
        if value is not None:
            self.alpha = value
        return self.alpha

    def deiconify(self):
        self.visible = True


class TestSmoothTreeScroll(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(SmoothTreeScroll, 'SmoothTreeScroll must be implemented')
        self.tree = FakeWidget()
        self.motion = SmoothTreeScroll(self.tree)
        self.addCleanup(self.motion.destroy)

    def test_wheel_eases_then_settles_without_changing_selection(self):
        self.assertEqual(self.tree.wheel(-120), 'break')
        self.assertEqual(self.tree.position['y'], 0.4)
        self.assertTrue(self.tree.jobs)
        self.tree.drain()
        self.assertAlmostEqual(self.tree.position['y'], 0.43)
        self.assertEqual(self.tree.selection, ('candidate-1',))

    def test_shift_wheel_scrolls_only_horizontally(self):
        self.tree.wheel(-120, shift=True)
        self.tree.drain()
        self.assertAlmostEqual(self.tree.position['x'], 0.43)
        self.assertEqual(self.tree.position['y'], 0.4)

    def test_horizontal_wheel_moves_in_small_steps_throughout_the_animation(self):
        self.tree.wheel(-120,shift=True)
        self.tree.drain()
        steps=[value for axis,value in self.tree.scroll_history if axis=='x']
        self.assertGreaterEqual(len(steps),6)
        distances=[b-a for a,b in zip([.4]+steps,steps)]
        self.assertTrue(all(0<step<.015 for step in distances),distances)
        self.assertAlmostEqual(steps[-1],.43)

    def test_fractional_trackpad_input_is_preserved_across_bursts(self):
        for _ in range(4):
            self.tree.wheel(-0.25)
            self.tree.drain()
        self.assertAlmostEqual(self.tree.position['y'], 0.41)

    def test_half_windows_delta_accumulates_instead_of_becoming_sixty_rows(self):
        self.motion.destroy()
        self.tree = FakeWidget('win32')
        self.motion = SmoothTreeScroll(self.tree)
        self.tree.wheel(-60)
        self.tree.wheel(-60)
        self.tree.drain()
        self.assertAlmostEqual(self.tree.position['y'], 0.43)

    def test_reduced_motion_scrolls_immediately(self):
        self.motion.destroy()
        self.motion = SmoothTreeScroll(self.tree, reduced_motion=lambda: True)
        self.tree.wheel(-120)
        self.assertAlmostEqual(self.tree.position['y'], 0.43)
        self.assertFalse(self.tree.jobs)

    def test_disabled_handler_allows_native_wheel_handling(self):
        self.motion.destroy()
        self.motion = SmoothTreeScroll(self.tree, enabled=lambda: False)
        self.assertIsNone(self.tree.wheel(-120))
        self.assertFalse(self.tree.jobs)

    def test_reduced_motion_keeps_fractional_trackpad_input(self):
        self.motion.destroy()
        self.motion = SmoothTreeScroll(self.tree, reduced_motion=lambda: True)
        for _ in range(4):
            self.tree.wheel(-0.25)
        self.assertAlmostEqual(self.tree.position['y'], 0.41)
        self.assertFalse(self.tree.jobs)

    def test_settled_axis_does_not_cancel_new_motion_after_external_move(self):
        self.tree.wheel(-120)
        self.tree.drain()
        self.tree.yview('moveto', 0.1)
        self.tree.wheel(-120, shift=True)
        self.tree.drain()
        self.assertAlmostEqual(self.tree.position['x'], 0.43)
        self.assertAlmostEqual(self.tree.position['y'], 0.1)

    def test_linux_buttons_and_shift_buttons_work(self):
        self.tree.wheel(button=5)
        self.tree.wheel(button=4, shift=True)
        self.tree.drain()
        self.assertAlmostEqual(self.tree.position['y'], 0.43)
        self.assertAlmostEqual(self.tree.position['x'], 0.37)

    def test_outward_wheel_at_edge_does_not_schedule_jitter(self):
        self.tree.position['y'] = 0.8
        self.tree.wheel(-120)
        self.assertFalse(self.tree.jobs)
        self.assertEqual(self.tree.position['y'], 0.8)

    def test_manual_scrollbar_move_cancels_pending_momentum(self):
        self.tree.wheel(-120)
        self.tree.yview('moveto', 0.1)
        self.tree.drain()
        self.assertAlmostEqual(self.tree.position['y'], 0.1)

    def test_direction_reversal_clears_old_momentum(self):
        self.tree.wheel(-120)
        self.tree.wheel(120)
        self.tree.drain()
        self.assertAlmostEqual(self.tree.position['y'], 0.37)

    def test_cancel_drops_motion_and_destroy_removes_only_owned_bindings(self):
        self.tree.wheel(-120)
        self.motion.cancel()
        outsider = self.tree.bind('<MouseWheel>', lambda event: None, add='+')
        self.assertFalse(self.tree.jobs)
        self.motion.destroy()
        self.assertEqual(list(self.tree.bindings), [('<MouseWheel>', outsider)])

    def test_destroyed_tree_stops_pending_callback_safely(self):
        self.tree.wheel(-120)
        self.tree.alive = False
        self.tree.drain()
        self.assertEqual(self.tree.position['y'], 0.4)


class TestWindowOpen(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(animate_window_open, 'animate_window_open must be implemented')
        self.win = FakeWidget()
        timer = patch('time.monotonic', lambda: self.win.now_ms / 1000)
        timer.start()
        self.addCleanup(timer.stop)

    def test_fade_settles_at_full_opacity_without_geometry_changes(self):
        cancel = animate_window_open(self.win)
        self.assertTrue(self.win.visible)
        self.assertLess(self.win.alpha, 1.0)
        self.win.drain()
        self.assertEqual(self.win.alpha, 1.0)
        self.assertTrue(callable(cancel))

    def test_late_tk_callback_finishes_fade_at_elapsed_deadline(self):
        animate_window_open(self.win, duration_ms=140)
        # The clock starts at the first frame, so setup work cannot eat the fade...
        self.win.now_ms = 200
        self.win.run_next()
        self.assertLess(self.win.alpha, 1.0)
        # ...but a later, late frame still ends it on time.
        self.win.now_ms = 400
        self.win.run_next()
        self.assertEqual(self.win.alpha, 1.0)
        self.assertFalse(self.win.jobs)

    def test_reduced_motion_shows_immediately(self):
        animate_window_open(self.win, reduce_motion=True)
        self.assertTrue(self.win.visible)
        self.assertEqual(self.win.alpha, 1.0)
        self.assertFalse(self.win.jobs)

    def test_cancel_restores_opacity_and_drops_callback(self):
        cancel = animate_window_open(self.win)
        cancel()
        self.assertEqual(self.win.alpha, 1.0)
        self.assertFalse(self.win.jobs)
        cancel()

    def test_destroy_during_fade_is_safe(self):
        animate_window_open(self.win)
        self.win.alive = False
        self.win.drain()
        self.assertFalse(self.win.jobs)

    def test_new_fade_cancels_previous_callback(self):
        animate_window_open(self.win)
        animate_window_open(self.win)
        self.assertEqual(len(self.win.jobs), 1)
        self.win.drain()
        self.assertEqual(self.win.alpha, 1.0)


class TestRealTkInteractions(unittest.TestCase):
    def test_horizontal_animation_has_pixel_sized_frames_on_real_treeview(self):
        try: root=tk.Tk()
        except tk.TclError as exc: self.skipTest(str(exc))
        self.addCleanup(root.destroy)
        root.geometry('360x180')
        tree=ttk.Treeview(root,columns=('wide',),show='headings')
        tree.column('wide',width=1200,stretch=False)
        tree.pack(fill='both',expand=True);root.update()
        tree.xview('moveto',.2)
        initial=tree.xview()[0]
        original=tree.xview
        frames=[]
        def measure(*args):
            result=original(*args)
            if args: frames.append(original()[0])
            return result
        tree.xview=measure
        motion=SmoothTreeScroll(tree)
        self.addCleanup(motion.destroy)
        tree.event_generate('<Shift-MouseWheel>',delta=-120)
        def settled():
            if motion._job is None: root.quit()
            else: root.after(5,settled)
        root.after(5,settled);root.after(1500,root.quit);root.mainloop()
        pixels=[(b-a)*1200 for a,b in zip([initial]+frames,frames) if b>a]
        self.assertGreaterEqual(len(pixels),5)
        self.assertLess(max(pixels),24)
        self.assertAlmostEqual((original()[0]-initial)*1200,60,delta=2)

    def test_wheel_axes_selection_fade_and_teardown_on_tk_event_loop(self):
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f'Tk display unavailable: {error}')
        self.addCleanup(root.destroy)
        root.geometry('360x180')
        tree = ttk.Treeview(root, columns=('wide',), show='headings')
        tree.column('wide', width=1200, stretch=False)
        tree.pack(fill='both', expand=True)
        for row in range(100):
            tree.insert('', 'end', iid=str(row), values=(f'Candidate {row}',))
        root.update()
        tree.yview('moveto', 0.25)
        tree.xview('moveto', 0.25)
        tree.selection_set('30')
        before_y, before_x = tree.yview()[0], tree.xview()[0]
        motion = SmoothTreeScroll(tree)
        self.addCleanup(motion.destroy)
        tree.event_generate('<MouseWheel>', delta=-120)
        tree.event_generate('<Shift-MouseWheel>', delta=-120)
        popup = tk.Toplevel(root)
        popup.geometry('220x100+30+30')
        popup.update_idletasks()
        geometry = popup.geometry()
        animate_window_open(popup)
        def settled():
            if popup.attributes('-alpha') == 1.0:
                root.quit()
            else:
                root.after(10, settled)
        root.after(10, settled)
        root.after(1200, root.quit)
        root.mainloop()
        self.assertGreater(tree.yview()[0], before_y)
        self.assertGreater(tree.xview()[0], before_x)
        self.assertEqual(tree.selection(), ('30',))
        self.assertAlmostEqual(popup.attributes('-alpha'), 1.0)
        self.assertEqual(popup.geometry(), geometry)
        # Turning off smoothing must leave Tk's native wheel behavior intact.
        motion.destroy()
        native_motion = SmoothTreeScroll(tree, enabled=lambda: False)
        self.addCleanup(native_motion.destroy)
        before_native = tree.yview()[0]
        tree.event_generate('<MouseWheel>', delta=-120)
        root.update()
        self.assertGreater(tree.yview()[0], before_native)
        self.assertEqual(tree.selection(), ('30',))
        # Destroy events must cancel jobs without Tcl invalid-command errors.
        native_motion.enabled = lambda: True
        tree.yview('moveto', 0.25)
        tree.event_generate('<MouseWheel>', delta=-120)
        tree.destroy()
        popup.destroy()
        root.update()


if __name__ == '__main__':
    unittest.main()
