"""Regression coverage for folder loading and responsive table updates."""
import asyncio
import contextlib
import threading
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import httpx
except ImportError:
    import httpx2 as httpx
import openai
import extractor
import test_extractor as legacy


class TestFolderPipeline(unittest.TestCase):
    setUp = legacy.TestExtractor.setUp
    tearDown = legacy.TestExtractor.tearDown
    _pipeline_mocks = legacy.TestMatchCacheAndAtomicWrites._pipeline_mocks
    _run = legacy.TestMatchCacheAndAtomicWrites._run

    def test_ai_missing_fields_preserve_contacts_read_from_resume(self):
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack)
            mocks['extract_text_from_file'].side_effect = None
            mocks['extract_text_from_file'].return_value = (
                'Alice Smith\nalice@example.com\n+1 212 555 1234\nNew York, USA')
            mocks['extract_data_with_ai_async'].side_effect = None
            mocks['extract_data_with_ai_async'].return_value = {
                'Name': 'Not Found', 'Email': 'Not Found', 'Phone': 'Not Found',
                'Country': 'United States', 'YearsOfExperience': '8'}
            self._run(jd='')
        row = self.app.candidates_by_file['resume1.pdf']
        self.assertEqual(row['Name'], 'Alice Smith')
        self.assertEqual(row['Email'], 'alice@example.com')
        self.assertEqual(row['Status'], 'Parsed')
        self.assertEqual(self.app.upload_queue.qsize(), 1)

    def test_extraction_uses_selected_model(self):
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack)
            self._run(jd='', model='gpt-5-mini')
            self.assertEqual(mocks['extract_data_with_ai_async'].call_args.kwargs['model'], 'gpt-5-mini')

    def test_rows_exist_while_file_reads_are_still_running(self):
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack)
            seen = []
            def read(path):
                seen.append('resume1.pdf' in self.app.candidates_by_file)
                return 'text'
            mocks['extract_text_from_file'].side_effect = read
            self._run(jd='')
        self.assertEqual(seen, [True])

    def test_empty_document_is_reported_as_read_error(self):
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack)
            mocks['extract_text_from_file'].side_effect = None
            mocks['extract_text_from_file'].return_value = '  \n  '
            self._run(jd='')
        self.assertEqual(self.app.candidates_by_file['resume1.pdf']['Status'], 'Read Error')

    def test_failed_ai_extraction_is_visible_and_retried_on_next_run(self):
        with contextlib.ExitStack() as stack:
            mocks = self._pipeline_mocks(stack)
            mocks['extract_data_with_ai_async'].side_effect = None
            mocks['extract_data_with_ai_async'].return_value = {
                k: 'API Error' for k in ('Name', 'Email', 'Phone', 'Country', 'YearsOfExperience')}
            self._run(jd='')
            row = self.app.candidates_by_file['resume1.pdf']
            self.assertIn('AI unavailable', row['Status'])
            self.assertTrue(row['Summary'])
            mocks['extract_data_with_ai_async'].reset_mock()
            self._run(jd='')
            self.assertEqual(mocks['extract_data_with_ai_async'].await_count, 1)


class TestApiFailures(unittest.TestCase):
    def test_invalid_key_is_not_retried(self):
        response = httpx.Response(401, request=httpx.Request('POST', 'https://api.openai.com/v1/chat/completions'))
        error = openai.AuthenticationError('Invalid API key', response=response, body=None)
        api_call = AsyncMock(side_effect=error)
        with patch('extractor.asyncio.sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaises(openai.AuthenticationError):
                asyncio.run(extractor.call_openai_with_retry(api_call))
        self.assertEqual(api_call.await_count, 1)
        sleep.assert_not_awaited()


@unittest.skipUnless(legacy._tk_available(), 'Tk is not available')
class TestIncrementalTable(unittest.TestCase):
    setUp = legacy.TestTableState.setUp
    tearDown = legacy.TestTableState.tearDown
    _add = legacy.TestTableState._add

    def test_refresh_updates_existing_row_without_deleting_it(self):
        row = self._add('a.pdf', 10)
        self.app.filter_table()
        self.tree.selection_set('a.pdf')
        self.tree.focus('a.pdf')
        row['Name'] = 'Updated Name'
        with patch.object(self.tree, 'delete', wraps=self.tree.delete) as delete:
            self.app.filter_table()
        self.assertEqual(self.tree.set('a.pdf', 'Name'), 'Updated Name')
        self.assertEqual(self.tree.focus(), 'a.pdf')
        self.assertEqual(self.tree.selection(), ('a.pdf',))
        delete.assert_not_called()

    def test_filter_removes_hidden_row_and_restores_it_when_cleared(self):
        self._add('a.pdf', 10)
        self._add('b.pdf', 90)
        self.app.filter_table()
        self.app.sv_min_score.get.return_value = '50'
        self.app.filter_table()
        self.assertEqual(self.tree.get_children(), ('b.pdf',))
        self.app.sv_min_score.get.return_value = ''
        self.app.filter_table()
        self.assertEqual(self.tree.get_children(), ('a.pdf', 'b.pdf'))


class TestEventBudget(unittest.TestCase):
    def test_large_event_backlog_yields_to_tk_then_completes(self):
        import queue
        app = MagicMock(spec=extractor.ExtractorApp)
        app.ui_events = queue.Queue()
        app._table_dirty = app._progress_dirty = False
        app._closing = False
        seen = []
        for i in range(1000):
            app.ui_events.put(lambda i=i: seen.append(i))
        extractor.ExtractorApp._drain_ui_events(app)
        self.assertGreater(len(seen), 0)
        self.assertLess(len(seen), 1000)
        while not app.ui_events.empty():
            extractor.ExtractorApp._drain_ui_events(app)
        self.assertEqual(seen, list(range(1000)))


class TestFolderDiscovery(unittest.TestCase):
    def setUp(self):
        self.app = MagicMock(spec=extractor.ExtractorApp)
        self.app._pipeline_running = False
        self.app._closing = False
        self.app.sv_api_key = MagicMock()
        self.app.sv_api_key.get.return_value = 'test-key'
        self.app.sv_dir = MagicMock()
        self.app.sv_dir.get.return_value = '/shared/resumes'
    def test_folder_enumeration_happens_off_main_thread(self):
        app = self.app
        app.sv_model = MagicMock()
        app.sv_model.get.return_value = 'gpt-5-mini'
        app.txt_jd = MagicMock()
        app.txt_jd.get.return_value = ''
        app.bv_jd_matching = MagicMock()
        app.bv_jd_matching.get.return_value = False
        app.btn_extract = MagicMock()
        app.chk_jd_matching = MagicMock()
        app.lbl_status = MagicMock()
        posted = []
        app._post = posted.append
        app._discover_resume_folder = extractor.ExtractorApp._discover_resume_folder.__get__(app)
        observed_threads = []
        main_thread = threading.get_ident()
        with tempfile.TemporaryDirectory() as folder:
            app.sv_dir.get.return_value = folder
            for name in ('one.pdf', 'two.docx', 'three.doc', 'four.txt', 'notes.csv'):
                with open(os.path.join(folder, name), 'w') as f:
                    f.write('resume')
            listdir = os.listdir
            def observe(path):
                observed_threads.append(threading.get_ident())
                return listdir(path)
            with patch('extractor.os.listdir', side_effect=observe):
                extractor.ExtractorApp.start_extraction(app)
                # Wait for the worker's posted completion without running Tk in it.
                import time
                deadline = time.monotonic() + 2
                while not posted and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertTrue(posted, 'folder scan did not finish')
            self.assertTrue(all(t != main_thread for t in observed_threads))
            posted[0]()
            args = app._begin_extraction.call_args.args
            self.assertEqual(set(args[1]), {'one.pdf', 'two.docx', 'three.doc', 'four.txt'})
            self.assertEqual(args[2], [('notes.csv', 'Unsupported file format')])

    def test_unavailable_network_folder_returns_a_recoverable_error(self):
        app = self.app
        posted = []
        app._post = posted.append
        with patch('extractor.os.listdir', side_effect=PermissionError('Access denied')):
            extractor.ExtractorApp._discover_resume_folder(app, '/shared/resumes', '', 'key', 'gpt-5', True)
        self.assertEqual(len(posted), 1)
        posted[0]()
        self.assertIn('Access denied', app._folder_load_failed.call_args.args[0])


@unittest.skipUnless(legacy._tk_available(), 'Tk is not available')
class TestWindowLoading(unittest.TestCase):
    def test_process_button_populates_real_window(self):
        import queue
        import time
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder, contextlib.ExitStack() as stack:
            with open(os.path.join(folder, 'resume.txt'), 'w') as f:
                f.write('Alice Smith\nalice@example.com\n+1 212 555 1234\nNew York, USA')
            stack.enter_context(patch.object(extractor, 'RESUME_CACHE_FILE', os.path.join(folder, 'cache.json')))
            stack.enter_context(patch.object(extractor.ExtractorApp, 'start_upload_worker',
                                             lambda app: setattr(app, 'upload_queue', queue.Queue())))
            stack.enter_context(patch.object(extractor.ExtractorApp, '_save_drive_cache'))
            stack.enter_context(patch.object(extractor, 'messagebox'))
            stack.enter_context(patch.object(extractor, 'OpenAI'))
            client = stack.enter_context(patch.object(extractor, 'AsyncOpenAI')).return_value
            client.beta.chat.completions.parse = AsyncMock(return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(parsed=extractor.ResumeData(
                    full_name='Alice Smith', email_address='alice@example.com', phone_number='2125551234',
                    country='United States', years_of_experience=8, source_filename='resume.txt')))]))
            app = extractor.ExtractorApp()
            errors = []
            app.report_callback_exception = lambda *exc: errors.append(exc)
            try:
                app.withdraw()
                app.bv_jd_matching.set(False)
                app.on_jd_matching_toggled()
                app.sv_dir.set(folder)
                app.sv_api_key.set('test-key')
                self.assertEqual(app._run_gen, 0)
                self.assertTrue(app.upload_queue.empty())
                app.btn_extract.invoke()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    app.update()
                    if app._run_gen == 1 and not app._pipeline_running:
                        break
                    time.sleep(0.01)
                self.assertEqual(errors, [])
                self.assertEqual(app._run_gen, 1)
                self.assertFalse(app._pipeline_running)
                self.assertEqual(app.tree.get_children(), ('resume.txt',))
                self.assertEqual(app.tree.set('resume.txt', 'Email'), 'alice@example.com')
                # Parsing finished, but the fake upload queue has not drained.
                self.assertEqual(app.entry_dir.cget('state'), 'disabled')
                self.assertEqual(app.btn_extract.cget('state'), 'disabled')
                self.assertIn('uploads continuing', app.lbl_status.cget('text').lower())
                self.assertEqual(app.upload_queue.qsize(), 1)
            finally:
                # Let scheduled CTk drawing callbacks finish before destroying Tk.
                app.update_idletasks()
                app.on_close()


if __name__ == '__main__':
    unittest.main()
