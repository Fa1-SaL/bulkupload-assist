"""Regressions for the UI/UX review: missing inputs are explained where they
occur, problems are marked without alarming colour, and states say why.

Actual Tk widgets run; no network requests are made.
"""
import unittest
from unittest.mock import patch

import extractor
from test_extractor import _tk_available
import test_ui_flow
from ui_interactions import Tooltip


@unittest.skipUnless(_tk_available(), 'Tk is not available')
class TestUxReview(unittest.TestCase):
    # Share the workflow fixture without collecting its tests a second time.
    _base = test_ui_flow.TestSimpleWorkflow
    setUp, pump, candidate = _base.setUp, _base.pump, _base.candidate
    running, finish_parsing = _base.running, _base.finish_parsing

    def test_matching_without_description_asks_for_it_instead_of_running(self):
        self.app.deiconify()
        self.app.sv_api_key.set('test-key')
        self.app.sv_dir.set(self.folder)
        self.app.bv_jd_matching.set(True)
        self.app.on_jd_matching_toggled()
        with patch.object(extractor.threading, 'Thread') as worker:
            self.app.start_extraction()
        worker.assert_not_called()
        self.app.update()
        self.assertTrue(self.app.lbl_jd_error.winfo_ismapped())
        self.app.txt_jd.insert('1.0', 'Python engineer')
        self.app._on_jd_edited()
        self.app.update()
        self.assertFalse(self.app.lbl_jd_error.winfo_ismapped())
        with patch.object(extractor.threading, 'Thread') as worker:
            self.app.start_extraction()
        worker.assert_called_once()

    def test_unticked_matching_processes_without_description(self):
        self.app.sv_api_key.set('test-key')
        self.app.sv_dir.set(self.folder)
        self.app.bv_jd_matching.set(False)
        self.app.on_jd_matching_toggled()
        with patch.object(extractor.threading, 'Thread') as worker:
            self.app.start_extraction()
        worker.assert_called_once()

    def test_jd_hint_shows_only_while_empty(self):
        self.app.deiconify()
        self.app.btn_extract.focus_set()
        self.app.update()
        self.assertTrue(self.app.lbl_jd_hint.winfo_ismapped())
        self.app.txt_jd.insert('1.0', 'Python')
        self.app._update_jd_hint()
        self.app.update()
        self.assertFalse(self.app.lbl_jd_hint.winfo_ismapped())

    def test_match_column_shows_percent_or_dash_and_rows_keep_normal_colour(self):
        row = self.candidate(link='Failed')
        other = self.candidate(filename='bob.pdf', link='Uploaded')
        other.update(Email='bob@example.com', MatchPercentage='Not evaluated')
        self.app.filter_table()
        self.assertEqual(self.app.tree.set('alice.pdf', 'MatchPct'), '80%')
        self.assertEqual(self.app.tree.set('bob.pdf', 'MatchPct'), '—')
        self.assertEqual(self.app.tree.tag_configure('problem', 'foreground'), '')
        self.assertEqual(str(self.app.tree.heading('MatchPct', 'anchor')), 'e')
        self.assertEqual(row['ResumeLink'], 'Failed')

    def test_first_run_checklist_ticks_completed_steps(self):
        self.app.deiconify()
        self.app.sv_api_key.set('')
        self.app.filter_table()
        self.app.update()
        self.assertTrue(self.app.frame_empty.winfo_ismapped())
        self.assertFalse(self.app._setup_steps['key'].cget('text').startswith('✓'))
        self.app.sv_api_key.set('test-key')
        self.assertTrue(self.app._setup_steps['key'].cget('text').startswith('✓'))
        self.candidate(link='Uploaded')
        self.app.filter_table()
        self.app.update()
        self.assertFalse(self.app.frame_empty.winfo_ismapped())

    def test_folder_preview_pluralises_and_links_skipped_files(self):
        gen = self.app._preview_generation
        self.app._show_folder_preview(gen, '1 resume found', [('notes.csv', 'Unsupported file format')], 1)
        self.app.update()
        self.assertTrue(self.app.btn_skipped.winfo_manager())
        self.assertEqual(self.app.btn_skipped.cget('text'), 'View 1 skipped file')
        self.app.show_skipped_files()
        self.assertIn('notes.csv', self.messages.showinfo.call_args.args[1])
        self.app._show_folder_preview(gen, '1 resume found', [], 1)
        self.assertFalse(self.app.btn_skipped.winfo_manager())

    def test_progress_bar_is_only_drawn_while_work_is_under_way(self):
        background = self.app.frame_bottom.cget('fg_color')
        self.assertEqual(self.app.progress_bar.cget('progress_color'), background)
        self.running()
        self.app._refresh_run_state()
        self.assertNotEqual(self.app.progress_bar.cget('progress_color'), background)

    def test_retry_and_cancel_are_inline_links_and_clear_hides_until_done(self):
        self.app.deiconify()
        self.app.update()
        self.assertFalse(self.app.btn_clear.winfo_ismapped())
        self.running()
        self.finish_parsing()
        self.app.handle_upload_failure('alice.pdf', 'quota', 1)
        self.app._refresh_run_state()
        self.app.update()
        self.assertIs(self.app.btn_retry.master, self.app.frame_status)
        self.assertTrue(self.app.btn_retry.winfo_ismapped())
        self.assertTrue(self.app.btn_clear.winfo_ismapped())
        self.assertIn('Upload failed: alice.pdf', self.app._activity_text())

    def test_failed_count_in_activity_filters_table_to_attention(self):
        self.candidate(link='Failed')
        ok = self.candidate(filename='bob.pdf', link='Uploaded')
        ok['Email'] = 'bob@example.com'
        self.app.filter_table()
        self.app._show_attention_rows()
        self.assertEqual(self.app.tree.get_children(), ('alice.pdf',))

    def test_no_drive_destination_line_in_main_window(self):
        self.assertFalse(hasattr(self.app, 'lbl_destination'))
        self.assertFalse(hasattr(self.app, 'btn_check_drive'))

    def test_job_description_box_animates_open_and_closed_without_arrow(self):
        self.assertFalse(hasattr(self.app, 'btn_jd_toggle'))
        self.app.deiconify()
        self.app.update()
        opened = int(self.app.frame_jd_clip.cget('height'))
        self.assertGreater(opened, 0)
        heights = []
        clip = self.app.frame_jd_clip
        configure = clip.configure
        def record(*args, **kwargs):
            if 'height' in kwargs:
                heights.append(kwargs['height'])
            return configure(*args, **kwargs)
        clip.configure = record
        self.app.bv_jd_matching.set(False)
        self.app.on_jd_matching_toggled()
        self.pump(0.4)
        del clip.configure
        # Several intermediate heights, not a jump from open to closed.
        self.assertGreaterEqual(len({h for h in heights if 0 < h < opened}), 2, heights)
        self.assertEqual(int(self.app.frame_jd_clip.cget('height')), 0)
        self.app.bv_jd_matching.set(True)
        self.app.on_jd_matching_toggled()
        self.pump(0.4)
        self.assertEqual(int(self.app.frame_jd_clip.cget('height')), opened)

    def test_click_outside_settings_closes_it_but_opening_click_does_not(self):
        self.app.deiconify()
        self.app.update()
        win = self.app._settings_window
        self.app.btn_settings.invoke()  # same path as pressing the button
        # The same press reaches the main window's binding; it must not close Settings.
        self.app._dismiss_settings_on_outside_click(type('E', (), {'widget': self.app.btn_settings._canvas})())
        self.assertTrue(win.is_open())
        self.pump(0.05)
        win.variables['recursive'].set(True)
        self.app._dismiss_settings_on_outside_click(type('E', (), {'widget': self.app.tree})())
        self.assertFalse(win.is_open())
        self.pump(0.3)
        self.assertFalse(win.winfo_viewable())
        self.assertFalse(win.get_draft()['recursive'])  # closing outside discards, like Cancel

    def test_settings_fade_out_can_be_interrupted_by_reopening(self):
        self.app.deiconify()
        self.app.toggle_settings()
        self.pump(0.3)
        self.app.toggle_settings()
        self.assertFalse(self.app._settings_window.is_open())
        self.app.toggle_settings()  # reopen during the fade
        self.pump(0.3)
        self.assertTrue(self.app._settings_window.is_open())
        self.assertEqual(float(self.app._settings_window.attributes('-alpha')), 1.0)

    def test_trackpad_scrolls_settings_page(self):
        win = self.app._settings_window
        win.show()
        win.geometry('480x420')
        self.pump(0.3)
        canvas = win.page._parent_canvas
        before = canvas.yview()[0]
        win.event_generate('<TouchpadScroll>', delta=(0 << 16) | (65536 - 40), when='now')
        self.app.update()
        self.assertGreater(canvas.yview()[0], before)

    def test_trackpad_scrolls_table_horizontally_on_every_event(self):
        self.app.deiconify()
        self.candidate(link='Uploaded')
        self.app.bv_all_columns.set(True)
        self.app.toggle_table_columns()
        self.app.filter_table()
        self.app.update()
        positions = []
        for _ in range(3):
            self.app.tree.event_generate('<TouchpadScroll>', delta=((65536 - 6) << 16), when='now')
            self.app.update()
            positions.append(self.app.tree.xview()[0])
        self.assertTrue(positions[0] > 0 and positions[0] < positions[1] < positions[2], positions)

    def test_fullscreen_uses_native_animation_on_mac_and_fades_elsewhere(self):
        with patch.object(self.app, 'attributes', return_value=0) as attributes, \
                patch.object(extractor.sys, 'platform', 'darwin'):
            self.app.toggle_fullscreen()
        attributes.assert_called_with('-fullscreen', True)
        with patch.object(self.app, 'attributes', return_value=0), \
                patch.object(extractor.sys, 'platform', 'win32'), \
                patch.object(extractor, 'fade_through') as fade:
            self.app.toggle_fullscreen()
        fade.assert_called_once()

    def test_details_show_tier_hide_missing_links_and_list_gaps_first(self):
        row = self.candidate(link='Uploaded')
        row.update(MissingRequirements=['Kubernetes'], Strengths=['Django'], Phone='2125551234')
        self.app.show_candidate_details(row)
        texts = []
        def walk(widget):
            if isinstance(widget, (extractor.ctk.CTkLabel, extractor.ctk.CTkButton)):
                texts.append(widget.cget('text'))
            for child in widget.winfo_children():
                walk(child)
        walk(self.app._details_window)
        self.assertIn('80% · Strong', texts)
        self.assertIn('212 555 1234', texts)
        self.assertNotIn('Open LinkedIn', texts)
        self.assertLess(texts.index('Missing requirements'), texts.index('Strengths'))

    def test_disabled_process_explains_why(self):
        self.running()
        self.app._refresh_run_state()
        self.assertEqual(self.app._process_disabled_reason(), 'Resumes are being processed.')
        tip = self.app._tooltips[0]
        self.assertEqual(tip._current_text(), 'Resumes are being processed.')


class TestUxHelpers(unittest.TestCase):
    def test_match_tier_and_display(self):
        self.assertEqual(extractor.match_tier(80)[0], 'Strong')
        self.assertEqual(extractor.match_tier(60)[0], 'Partial')
        self.assertEqual(extractor.match_tier(10)[0], 'Weak')
        self.assertEqual(extractor.match_display('86'), '86%')
        self.assertEqual(extractor.match_display('Error'), '—')

    def test_shortcut_labels_follow_platform(self):
        self.assertEqual(extractor.shortcut_label('open', 'darwin'), '⌘O')
        self.assertEqual(extractor.shortcut_label('process', 'darwin'), '⌘↩')
        self.assertEqual(extractor.shortcut_label('open', 'win32'), 'Ctrl+O')
        self.assertEqual(extractor.shortcut_label('process', 'win32'), 'Ctrl+Enter')
        self.assertEqual(extractor.shortcut_label('fullscreen', 'linux'), 'F11')

    def test_phone_display_groups_digits_only(self):
        self.assertEqual(extractor.format_phone_display('2125551234'), '212 555 1234')
        self.assertEqual(extractor.format_phone_display('12125551234'), '+1 212 555 1234')
        self.assertEqual(extractor.format_phone_display('+44 20 7946'), '+44 20 7946')

    @unittest.skipUnless(_tk_available(), 'Tk is not available')
    def test_tooltip_text_none_shows_nothing(self):
        root = extractor.tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        button = extractor.tk.Button(root)
        tip = Tooltip(button, lambda: None)
        tip.show()
        self.assertIsNone(tip._tip)


if __name__ == '__main__':
    unittest.main()
