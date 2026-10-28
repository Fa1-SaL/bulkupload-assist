"""Headless checks for export availability after partial processing failures."""
import unittest
from types import SimpleNamespace

from extractor import ExtractorApp


class StateWidget:
    def __init__(self):
        self.values = {}

    def configure(self, **values):
        self.values.update(values)

    def cget(self, key):
        return self.values[key]


def candidate(**changes):
    row = dict(SourceFilename='ada.pdf', Name='Ada', Email='ada@example.test',
               Phone='1234567890', Country='India', YearsOfExperience='5',
               ResumeLink='Failed', ResumeDriveURL='', Parsed=True,
               Status='Parsed', UploadError='offline')
    row.update(changes)
    return row


def settled_app(rows):
    widgets = {name: StateWidget() for name in
               ('chk_jd_matching', 'btn_extract', 'btn_clear', 'btn_retry',
                'btn_cancel_uploads', 'btn_export', 'lbl_status')}
    app = SimpleNamespace(**widgets)
    app.extracted_data = rows
    app._visible_candidates = rows
    app.preferences = {'export_missing': False}
    app._pref = lambda key: app.preferences.get(key, False)
    app._has_pending_uploads = lambda: False
    app._set_source_controls = lambda state: None
    app._sync_action_buttons = lambda: None
    app._pipeline_running = False
    app._pipeline_finished = True
    app._pipeline_error = None
    app._connection_busy = False
    app._processed_count = len(rows)
    app.completed_uploads = 0
    app.failed_uploads = 1
    app.cancelled_uploads = 0
    app._processing_issue_count = 0
    app.skipped_files = []
    return app


class ExportAvailabilityTests(unittest.TestCase):
    def test_failed_upload_leaves_export_action_available(self):
        app = settled_app([candidate()])
        ExtractorApp._refresh_run_state(app)
        self.assertEqual(app.btn_export.cget('state'), 'normal')

    def test_dialog_can_include_failed_upload_without_changing_default(self):
        app = settled_app([candidate()])
        self.assertEqual(ExtractorApp._eligible_rows(app), [])
        self.assertEqual(len(ExtractorApp._eligible_rows(app, include_missing=True)), 1)
        self.assertFalse(app.preferences['export_missing'])

    def test_unreadable_or_pending_rows_do_not_enable_export(self):
        app = settled_app([candidate(Parsed=False, Status='Read Error'),
                           candidate(SourceFilename='pending.pdf', ResumeLink='Pending')])
        ExtractorApp._refresh_run_state(app)
        self.assertEqual(app.btn_export.cget('state'), 'disabled')


if __name__ == '__main__':
    unittest.main()
