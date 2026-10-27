"""User-visible regressions for deliberate processing and trustworthy run status.

External parsing/upload work is replaced; actual Tk widgets and UI handlers run.
"""
import contextlib
import os
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import extractor
from test_extractor import _tk_available


@unittest.skipUnless(_tk_available(), 'Tk is not available')
class TestSimpleWorkflow(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.messages = self.stack.enter_context(patch.object(extractor, 'messagebox'))
        def upload_state(app):
            app.upload_queue = queue.Queue()
            app._drive_creds_lock = threading.Lock()
            app._drive_creds = None
            app._drive_auth_failed = False
        self.stack.enter_context(patch.object(extractor.ExtractorApp, 'start_upload_worker', upload_state))
        self.stack.enter_context(patch.object(extractor.ExtractorApp, '_save_drive_cache'))
        self.app = extractor.ExtractorApp()
        self.app.withdraw()
        self.addCleanup(self.app.on_close)

    def pump(self, seconds=0.15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.01)

    def candidate(self, filename='alice.pdf', link='Pending'):
        row = dict(SourceFilename=filename, Name='Alice', Email='alice@example.com',
                   Phone='12125551234', Country='United States', YearsOfExperience='8',
                   Status='Parsed', ResumeLink=link, MatchPercentage=80)
        self.app.extracted_data.append(row)
        self.app.candidates_by_file[filename] = row
        return row

    def running(self):
        self.app._run_gen = 1
        self.app._pipeline_running = True
        self.app.total_uploads = 1
        self.candidate()

    def finish_parsing(self):
        self.app.extraction_finished(1, self.folder, ['alice.pdf'], time.time())
        self.app.update_upload_progress_ui()

    def test_folder_and_key_edits_only_preview_files(self):
        for name in ('one.pdf', 'two.docx', 'ignored.csv'):
            with open(os.path.join(self.folder, name), 'w') as f:
                f.write('fixture')
        self.app.sv_dir.set(self.folder)
        self.app.sv_api_key.set('test-key')
        with patch.object(extractor, 'AsyncOpenAI') as client:
            self.pump(1.3)
        self.assertEqual(self.app._run_gen, 0)
        self.assertTrue(self.app.upload_queue.empty())
        self.assertFalse(self.app._pipeline_running)
        client.assert_not_called()
        self.assertIn('2 resumes', self.app.lbl_status.cget('text'))

    def test_parsing_completion_waits_for_upload_then_enables_export(self):
        self.running()
        self.finish_parsing()
        self.assertIn('uploads continuing', self.app.lbl_status.cget('text').lower())
        self.assertEqual(self.app.btn_export.cget('state'), 'disabled')
        self.assertEqual(self.app.btn_extract.cget('state'), 'disabled')
        self.messages.showinfo.assert_not_called()
        self.app.handle_upload_success('alice.pdf', 'https://drive.google.com/file/d/test/view', 1)
        self.app.update_upload_progress_ui()
        self.assertTrue(self.app.lbl_status.cget('text').startswith('Complete —'))
        self.assertEqual(self.app.btn_export.cget('state'), 'normal')
        self.assertIn('1', self.app.btn_export.cget('text'))
        self.assertEqual(self.app.btn_extract.cget('state'), 'normal')

    def test_failure_and_retry_return_to_pending_then_complete(self):
        self.running()
        self.finish_parsing()
        self.app.handle_upload_failure('alice.pdf', RuntimeError('offline'), 1)
        self.app.update_upload_progress_ui()
        self.assertIn('Complete with issues', self.app.lbl_status.cget('text'))
        self.assertEqual(self.app.btn_export.cget('state'), 'disabled')
        self.app.sv_dir.set(self.folder)
        self.app.retry_failed_uploads()
        self.app.update_upload_progress_ui()
        self.assertIn('uploads continuing', self.app.lbl_status.cget('text').lower())
        self.app.handle_upload_success('alice.pdf', 'https://drive.google.com/file/d/test/view', 1)
        self.app.update_upload_progress_ui()
        self.assertTrue(self.app.lbl_status.cget('text').startswith('Complete —'))

    def test_cancelled_upload_is_an_issue_and_retains_parsed_results(self):
        self.running()
        self.finish_parsing()
        self.app.handle_upload_cancelled('alice.pdf', 1)
        self.app.update_upload_progress_ui()
        self.assertIn('Complete with issues', self.app.lbl_status.cget('text'))
        self.assertIn('1 cancelled', self.app.lbl_status.cget('text'))
        self.assertEqual(len(self.app.extracted_data), 1)
        self.assertEqual(self.app.btn_retry.cget('state'), 'normal')

    def test_invalid_candidate_cannot_enable_export(self):
        self.app._run_gen = 1
        self.app._pipeline_running = True
        row = self.candidate(link='Not uploaded')
        row['Phone'] = 'Not Found'
        row['Status'] = 'Missing Contact Information'
        self.finish_parsing()
        self.assertIn('Complete with issues', self.app.lbl_status.cget('text'))
        self.assertEqual(self.app.btn_export.cget('state'), 'disabled')

    def test_new_run_cannot_discard_pending_uploads(self):
        self.running()
        self.finish_parsing()
        self.app.sv_dir.set(self.folder)
        self.app.sv_api_key.set('test-key')
        with patch.object(extractor.threading, 'Thread') as worker:
            self.app.start_extraction()
        worker.assert_not_called()
        self.assertEqual(self.app._run_gen, 1)
        self.assertIn('alice.pdf', self.app.candidates_by_file)

    def test_pipeline_failure_is_not_overwritten_by_upload_completion(self):
        self.running()
        self.app._pipeline_failed('Connection lost')
        self.app.handle_upload_success('alice.pdf', 'https://drive.google.com/file/d/test/view', 1)
        self.app.update_upload_progress_ui()
        self.assertIn('Processing failed', self.app.lbl_status.cget('text'))
        self.assertIn('Connection lost', self.app.lbl_status.cget('text'))

    def test_activity_keeps_results_and_primary_actions_in_place(self):
        self.app.deiconify()
        self.app.geometry('1200x700')
        self.app.update()
        before = self.app.tree.winfo_height()
        self.app.toggle_activity()
        self.app.update()
        self.assertGreaterEqual(self.app.tree.winfo_height(), before)
        bottom = self.app.btn_extract.winfo_rooty() + self.app.btn_extract.winfo_height()
        self.assertLessEqual(bottom, self.app.winfo_rooty() + self.app.winfo_height())

    def test_stale_folder_preview_cannot_replace_current_folder_count(self):
        self.app.sv_dir.set('/old')
        old = self.app._preview_generation
        self.app.sv_dir.set('/new')
        current = self.app._preview_generation
        self.app._show_folder_preview(current, '2 resumes found')
        self.app._show_folder_preview(old, '99 resumes found')
        self.assertEqual(self.app.lbl_status.cget('text'), '2 resumes found')

    def test_turning_off_matching_disables_and_ignores_score_filter(self):
        self.app.deiconify()
        row = self.candidate(link='Uploaded')
        row['MatchPercentage'] = 'N/A'
        self.app.sv_min_score.set('80')
        self.app.update()
        self.assertEqual(self.app.tree.get_children(), ())
        self.app.bv_jd_matching.set(False)
        self.app.on_jd_matching_toggled()
        self.app.update()
        # Disabled, not hidden, so the filter bar keeps its layout.
        self.assertTrue(self.app.seg_min_score.winfo_ismapped())
        self.assertEqual(self.app.seg_min_score.cget('state'), 'disabled')
        self.assertEqual(self.app.tree.get_children(), ('alice.pdf',))
        self.app.bv_jd_matching.set(True)
        self.app.on_jd_matching_toggled()
        self.app.update()
        self.assertEqual(self.app.seg_min_score.cget('state'), 'normal')
        self.assertEqual(self.app.tree.get_children(), ())

    def test_empty_search_shows_hint_without_filtering_candidates(self):
        self.app.deiconify()
        self.candidate(link='Uploaded')
        self.app.filter_table()
        self.app.btn_extract.focus_set()
        self.app.update()
        self.assertEqual(self.app.sv_search.get(), '')
        self.assertTrue(self.app.lbl_search_hint.winfo_ismapped())
        self.assertEqual(self.app.tree.get_children(), ('alice.pdf',))
        self.app.sv_search.set('nobody')
        self.app.update()
        self.assertFalse(self.app.lbl_search_hint.winfo_ismapped())
        self.assertEqual(self.app.tree.get_children(), ())
        self.app.sv_search.set('')
        self.app.update()
        self.assertTrue(self.app.lbl_search_hint.winfo_ismapped())
        self.assertEqual(self.app.tree.get_children(), ('alice.pdf',))

    def thumb_visible(self, bar):
        return bar.cget('button_color') == extractor.SCROLL_THUMB

    def test_scrollbar_thumbs_only_appear_when_content_overflows(self):
        self.app.deiconify()
        self.candidate(link='Uploaded')
        self.app.filter_table()
        self.app.update()
        width = self.app.tree.winfo_width()
        self.assertFalse(self.thumb_visible(self.app.scrollbar_x))
        self.assertFalse(self.thumb_visible(self.app.scrollbar_y))
        self.app.bv_all_columns.set(True)
        self.app.toggle_table_columns()
        self.app.update()
        self.assertTrue(self.thumb_visible(self.app.scrollbar_x))
        for index in range(50):
            row = self.candidate(filename=f'resume{index}.pdf', link='Uploaded')
            row['Email'] = f'alice{index}@example.com'
        self.app.filter_table()
        self.app.update()
        self.assertTrue(self.thumb_visible(self.app.scrollbar_y))
        # Space is reserved, so showing the thumb never resizes the table.
        self.assertEqual(self.app.tree.winfo_width(), width)

    def test_compact_table_keeps_full_data_and_details_accessible(self):
        row = self.candidate(link='Uploaded')
        self.app.filter_table()
        self.assertEqual(tuple(self.app.tree.cget('displaycolumns')),
                         ('Name', 'MatchPct', 'YearsExp', 'Country', 'ResumeLink'))
        self.assertEqual(self.app.tree.set('alice.pdf', 'Email'), 'alice@example.com')
        self.app.tree.selection_set('alice.pdf')
        self.app.on_row_double_click(None)
        first = self.app._details_window
        self.app.on_row_double_click(None)
        self.assertIs(self.app._details_window, first)
        self.assertTrue(first.winfo_exists())
        self.app.bv_all_columns.set(True)
        self.app.toggle_table_columns()
        self.assertIn('Status', self.app.tree.cget('displaycolumns'))
        self.assertEqual(self.app.tree.selection(), ('alice.pdf',))

    def test_action_bar_shows_only_usable_actions_in_fixed_slots(self):
        self.app.deiconify()
        self.app.update()
        self.assertFalse(self.app.btn_export.winfo_ismapped())
        self.assertFalse(self.app.btn_retry.winfo_ismapped())
        process_x = self.app.btn_extract.winfo_rootx()
        self.running()
        self.finish_parsing()
        self.app.handle_upload_success('alice.pdf', 'https://drive.google.com/file/d/test/view', 1)
        self.app.update_upload_progress_ui()
        self.app.update()
        self.assertTrue(self.app.btn_export.winfo_ismapped())
        self.assertEqual(self.app.btn_export.cget('fg_color'), extractor.ACCENT)
        self.assertEqual(self.app.btn_extract.winfo_rootx(), process_x)

    def test_matching_run_ranks_by_match_and_marks_sorted_column(self):
        self.running()
        low = self.candidate(filename='bob.pdf')
        low.update(Name='Bob', Email='bob@example.com', MatchPercentage=40)
        self.app.filter_table()
        self.finish_parsing()
        self.assertEqual(self.app.tree.get_children(), ('alice.pdf', 'bob.pdf'))
        self.assertTrue(self.app.tree.heading('MatchPct', 'text').endswith('▼'))

    def test_failed_upload_is_marked_in_table(self):
        self.candidate(link='Failed')
        self.app.filter_table()
        self.assertEqual(self.app.tree.set('alice.pdf', 'ResumeLink'), '✕ Failed')
        self.assertIn('problem', self.app.tree.item('alice.pdf', 'tags'))

    def test_collapsed_job_description_shows_summary(self):
        self.app.txt_jd.insert('1.0', 'Senior Python Engineer\nBuild APIs')
        self.app.toggle_jd()
        self.assertIn('Senior Python Engineer', self.app.lbl_jd_summary.cget('text'))
        self.assertIn('5 words', self.app.lbl_jd_summary.cget('text'))
        self.app.bv_jd_matching.set(False)
        self.app.on_jd_matching_toggled()
        self.assertEqual(self.app.btn_jd_toggle.cget('state'), 'disabled')
        self.assertFalse(self.app.frame_jd_body.winfo_manager())

    def test_export_success_reports_in_status_line(self):
        self.app._export_succeeded('/tmp/exports', 3, 1)
        self.assertIn('Exported 3 candidates', self.app.lbl_status.cget('text'))
        self.assertTrue(self.app.btn_reveal.winfo_manager())
        self.messages.showinfo.assert_not_called()


if __name__ == '__main__':
    unittest.main()
