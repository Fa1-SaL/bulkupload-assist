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
        self.stack.enter_context(patch.object(extractor, 'PREFERENCES_FILE', os.path.join(self.folder, 'prefs.json')))
        self.stack.enter_context(patch.object(extractor, 'REVIEW_FILE', os.path.join(self.folder, 'review.json')))
        self.messages = self.stack.enter_context(patch.object(extractor, 'messagebox'))
        class MemorySecret:
            def __init__(self, **kwargs): self.value = None
            def get(self): return self.value
            def set(self, value): self.value = value
            def delete(self): self.value = None
        self.stack.enter_context(patch.object(extractor, 'SecureApiKeyStore', MemorySecret))
        def upload_state(app):
            app.upload_queue = queue.Queue()
            app._drive_creds_lock = threading.Lock()
            app._drive_creds = None
            app._drive_auth_failed = False
        self.stack.enter_context(patch.object(extractor.ExtractorApp, 'start_upload_worker', upload_state))
        self.stack.enter_context(patch.object(extractor.ExtractorApp, '_save_drive_cache'))
        self.app = extractor.ExtractorApp()
        self.app.withdraw()
        self.addCleanup(lambda:self.app.on_close(force=True))

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

    def test_clear_only_settled_batch_and_ignores_late_callbacks(self):
        self.running()
        self.app.clear_batch()
        self.assertEqual(len(self.app.extracted_data),1)
        self.finish_parsing()
        self.app.handle_upload_success('alice.pdf','https://drive.test/a',1)
        self.app.update_upload_progress_ui()
        generation=self.app._run_gen
        self.app.clear_batch()
        self.assertGreater(self.app._run_gen,generation)
        self.assertEqual(self.app.extracted_data,[])
        self.assertEqual(self.app.btn_extract.cget('text'),'Process resumes')
        self.app.handle_upload_success('alice.pdf','https://drive.test/late',generation)
        self.assertEqual(self.app.completed_uploads,0)
        self.assertEqual(self.app.progress_bar.get(),0)

    def test_primary_button_exposes_failure_retry_then_done(self):
        self.running()
        self.app.update_upload_progress_ui()
        self.assertEqual(self.app.btn_extract.cget('text'),'Processing…')
        self.finish_parsing()
        self.assertEqual(self.app.btn_extract.cget('text'),'Uploading…')
        self.app.handle_upload_failure('alice.pdf',RuntimeError('offline'),1)
        self.app.update_upload_progress_ui()
        self.assertEqual(self.app.btn_extract.cget('text'),'Retry uploads')
        with patch.object(self.app,'retry_failed_uploads') as retry:
            self.app.btn_extract.invoke()
        retry.assert_called_once()
        self.app.handle_upload_success('alice.pdf','https://drive.test/a',1)
        self.app.failed_uploads=0
        self.app.update_upload_progress_ui()
        self.assertEqual(self.app.btn_extract.cget('text'),'Done ✓')

    def test_close_active_batch_requires_confirmation(self):
        self.running()
        self.messages.askyesno.return_value=False
        self.app.on_close()
        self.assertFalse(self.app._closing)
        self.messages.askyesno.assert_called_once()

    def test_regular_export_match_column_uses_batch_snapshot(self):
        import csv
        row=self.candidate(link='Uploaded')
        row['ResumeDriveURL']='https://drive.test/a'
        self.app.preferences['regular_export']=True
        for matched in (False,True):
            self.app._batch_matching_enabled=matched
            self.app.bv_jd_matching.set(not matched)
            path=os.path.join(self.folder,'out.csv')
            with patch.object(extractor.filedialog,'asksaveasfilename',return_value=path):
                self.app.execute_final_export(type('Dialog',(),{'destroy':lambda self:None})(),[row],'','','','','Yes')
            with open(os.path.join(self.folder,'out_candidate_data_batch_1.csv')) as f:
                header=next(csv.reader(f))
            self.assertEqual('match_percentage' in header,matched)
            self.assertNotIn('job_id',header)

    def test_legacy_destination_settings_cannot_redirect_uploads(self):
        prefs=dict(self.app.preferences,drive_folder_id='not a folder!?')
        self.app._save_settings(prefs,'test-key')
        self.assertNotIn('drive_folder_id', self.app.preferences)
        self.assertFalse(self.app._pipeline_running)

    def test_upload_code_uses_secure_store_and_rolls_back_on_failed_save(self):
        prefs = dict(self.app.preferences)
        self.app._save_settings(prefs, 'test-key', 'first-code')
        self.assertEqual(self.app._upload_access_store.get(), 'first-code')
        self.assertEqual(self.app._upload_access_code, 'first-code')
        with patch.object(self.app._preference_store, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.app._save_settings(prefs, 'test-key', 'second-code')
        self.assertEqual(self.app._upload_access_store.get(), 'first-code')
        self.assertEqual(self.app._upload_access_code, 'first-code')

    def test_failed_preference_save_restores_previous_secure_key(self):
        prefs=dict(self.app.preferences,remember_api_key=True)
        with patch.object(self.app._secret_store,'get',return_value='old-key'), \
             patch.object(self.app._secret_store,'set') as set_key, \
             patch.object(self.app._preference_store,'save',side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.app._save_settings(prefs,'new-key')
        self.assertEqual([c.args[0] for c in set_key.call_args_list],['new-key','old-key'])
        self.assertFalse(self.app.preferences['remember_api_key'])

    def test_stale_candidate_retry_cannot_restart_cleared_batch(self):
        row=self.candidate(link='Not uploaded')
        generation=self.app._run_gen
        self.app.clear_batch()
        with patch.object(self.app,'extraction_thread') as worker:
            self.app.retry_candidate(row,generation)
        worker.assert_not_called()
        self.assertFalse(self.app._pipeline_running)

    def test_connection_operation_blocks_new_processing_until_finished(self):
        with patch.object(extractor.threading,'Thread'):
            self.app._reconnect_drive_account()
        self.assertTrue(self.app._connection_busy)
        self.assertEqual(self.app.btn_extract.cget('state'),'disabled')
        self.app.sv_api_key.set('test-key')
        with patch.object(self.app,'_discover_resume_folder') as scan:
            self.app.start_extraction()
        scan.assert_not_called()
        self.app._finish_drive_connection()
        self.assertFalse(self.app._connection_busy)

    def test_retry_uses_fixed_owner_destination_and_keeps_batch_source(self):
        self.running()
        self.finish_parsing()
        self.app.handle_upload_failure('alice.pdf',RuntimeError('denied'),1)
        self.app.update_upload_progress_ui()
        self.app._batch_folder=self.folder
        self.app.sv_dir.set('/different-source')
        self.app._batch_drive_folder='old-folder'
        self.app.preferences['drive_folder_id']='new-folder'
        self.app.retry_failed_uploads()
        import drive_uploader
        self.assertEqual(self.app._batch_drive_folder,drive_uploader.DRIVE_FOLDER_ID)
        job=self.app.upload_queue.get_nowait()
        self.assertEqual(job[2],os.path.join(self.folder,'alice.pdf'))

    def test_secondary_retry_obeys_connection_lock(self):
        self.running()
        self.finish_parsing()
        self.app.handle_upload_failure('alice.pdf',RuntimeError('offline'),1)
        self.app._connection_busy=True
        self.app.update_upload_progress_ui()
        self.assertEqual(self.app.btn_retry.cget('state'),'disabled')
        self.app.retry_failed_uploads()
        self.assertTrue(self.app.upload_queue.empty())

    def test_export_dialog_holds_focus_to_prevent_stale_edits(self):
        self.app.deiconify()
        row=self.candidate(link='Uploaded')
        row['ResumeDriveURL']='https://drive.test/a'
        self.app.filter_table()
        self.app.open_final_export_dialog()
        dialog=self.app._batch_windows[-1]
        self.assertEqual(self.app.grab_current(),dialog)
        dialog.destroy()

    def test_fixed_text_size_preserves_hidden_controls_and_settings_window_is_reused(self):
        self.app.deiconify()
        self.app.bv_jd_matching.set(False)
        self.app.on_jd_matching_toggled()
        self.app._apply_text_scale(1.15)
        self.app.update()
        self.assertFalse(self.app.btn_export.winfo_ismapped())
        self.assertFalse(self.app.btn_retry.winfo_ismapped())
        self.assertFalse(self.app.btn_reveal.winfo_ismapped())
        self.assertFalse(self.app.frame_min_score.winfo_ismapped())
        self.assertFalse(self.app.frame_jd_body.winfo_ismapped())
        self.app.toggle_settings()
        win=self.app._settings_window
        win._select_page('Export')
        self.app._apply_text_scale(1.3)
        self.app.update()
        # One scrolling page: every section stays visible, and the jump target is kept.
        self.assertTrue(all(page.winfo_viewable() for page in win._pages.values()))
        self.assertEqual(win._active_page, 'Export')
        self.assertGreater(win.page._parent_canvas.yview()[0], 0)
        win.cancel()
        self.app.toggle_settings()
        self.assertIs(self.app._settings_window,win)
        self.assertEqual(self.app.tree.cget('displaycolumns'),extractor.COMPACT_COLUMNS)
        import customtkinter
        self.assertEqual(customtkinter.ScalingTracker.widget_scaling,1.0)

    def test_horizontal_wheel_over_table_works_with_window_focus(self):
        self.app.geometry('1200x700')  # Guarantee overflow on wider/high-DPI screens.
        self.app.deiconify()
        self.candidate(link='Uploaded')
        self.app.bv_all_columns.set(True)
        self.app.toggle_table_columns()
        self.app.filter_table()
        self.app.update()
        self.app.tree.xview('moveto',.15)
        before=self.app.tree.xview()[0]
        self.app.event_generate('<Shift-MouseWheel>',delta=-120,
            rootx=self.app.tree.winfo_rootx()+40,rooty=self.app.tree.winfo_rooty()+60)
        self.pump(.2)
        self.assertGreater(self.app.tree.xview()[0],before)
        settled=self.app.tree.xview()[0]
        self.app.event_generate('<Shift-MouseWheel>',delta=-120,
            rootx=self.app.winfo_rootx()+10,rooty=self.app.winfo_rooty()+10)
        self.pump(.2)
        self.assertAlmostEqual(self.app.tree.xview()[0],settled)

    def test_wheel_over_horizontal_scrollbar_is_eased_once(self):
        self.app.geometry('1200x700')
        self.app.deiconify();self.candidate(link='Uploaded')
        self.app.bv_all_columns.set(True);self.app.toggle_table_columns()
        self.app.filter_table();self.app.update()
        self.app.tree.xview('moveto',.15)
        before=self.app.tree.xview()[0]
        self.app.scrollbar_x._canvas.event_generate('<MouseWheel>',delta=-120,
            rootx=self.app.scrollbar_x.winfo_rootx()+40,rooty=self.app.scrollbar_x.winfo_rooty()+2)
        self.assertAlmostEqual(self.app.tree.xview()[0],before)
        self.pump(.2)
        moved=self.app.tree.xview()[0]-before
        self.assertGreater(moved,0)
        self.assertLess(moved,.1)

    def test_csv_completion_is_reported_when_folder_preference_cannot_save(self):
        from unittest.mock import MagicMock
        row=self.candidate(link='Uploaded')
        row['ResumeDriveURL']='https://drive.test/a'
        with patch.object(extractor.filedialog,'asksaveasfilename',return_value=os.path.join(self.folder,'out.csv')), \
             patch.object(self.app._preference_store,'save',side_effect=OSError('disk full')):
            self.app.execute_final_export(MagicMock(),[row],'J1','','','','Yes')
        self.assertTrue(os.path.exists(os.path.join(self.folder,'J1_candidate_data_batch_1.csv')))
        self.assertIn('CSV generated',self.app.lbl_status.cget('text'))
        self.assertIn('preference could not be saved',self.app.lbl_status.cget('text'))

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
        self.assertEqual(self.app.btn_extract.cget('text'), 'Done ✓')

    def test_failure_and_retry_return_to_pending_then_complete(self):
        self.running()
        self.finish_parsing()
        self.app.handle_upload_failure('alice.pdf', RuntimeError('offline'), 1)
        self.app.update_upload_progress_ui()
        self.assertIn('Complete with issues', self.app.lbl_status.cget('text'))
        self.assertEqual(self.app.btn_export.cget('state'), 'normal')
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
        self.assertEqual(self.app.btn_export.cget('state'), 'normal')

    def test_incomplete_candidate_keeps_export_available(self):
        self.app._run_gen = 1
        self.app._pipeline_running = True
        row = self.candidate(link='Not uploaded')
        row['Phone'] = 'Not Found'
        row['Status'] = 'Missing Contact Information'
        self.finish_parsing()
        self.assertIn('Complete with issues', self.app.lbl_status.cget('text'))
        self.assertEqual(self.app.btn_export.cget('state'), 'normal')

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

    def test_turning_off_matching_hides_and_ignores_score_filter(self):
        self.app.deiconify()
        row = self.candidate(link='Uploaded')
        row['MatchPercentage'] = 'N/A'
        self.app.sv_min_score.set('80')
        self.app.update()
        self.assertEqual(self.app.tree.get_children(), ())
        self.app.bv_jd_matching.set(False)
        self.app.on_jd_matching_toggled()
        self.app.update()
        # Match controls are hidden when matching is off.
        self.assertFalse(self.app.seg_min_score.winfo_ismapped())
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
        self.app.geometry('1200x700')
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
        self.pump(0.4)  # the box folds away with a short animation
        self.assertEqual(int(self.app.frame_jd_clip.cget('height')), 0)
        self.assertFalse(self.app.frame_jd_body.winfo_manager())

    def test_export_success_reports_in_status_line(self):
        self.app._export_succeeded('/tmp/exports', 3, 1)
        self.assertIn('CSV generated for 3 candidates', self.app.lbl_status.cget('text'))
        self.assertTrue(self.app.btn_reveal.winfo_manager())
        self.messages.showinfo.assert_not_called()


if __name__ == '__main__':
    unittest.main()
