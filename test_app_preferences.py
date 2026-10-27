"""Persistence and transactional Settings regression coverage."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_preferences as prefs


class PreferenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "settings.json"
        self.store = prefs.PreferenceStore(self.path)

    def test_missing_and_corrupt_files_return_independent_defaults(self):
        first = self.store.load()
        first["column_widths"]["Name"] = 999
        self.assertEqual({}, self.store.load()["column_widths"])
        self.path.write_text("{truncated", encoding="utf-8")
        self.assertEqual(prefs.DEFAULT_PREFERENCES, self.store.load())
        self.path.write_text('["not an object"]', encoding="utf-8")
        self.assertEqual(prefs.DEFAULT_PREFERENCES, self.store.load())

    def test_normalizes_fields_and_excludes_secrets(self):
        saved = self.store.save({"text_scale": 1.15, "recursive": "false",
            "model": " gpt-6-luna ", "api_key": "sensitive", "access_token": "sensitive",
            "remember_api_key": True, "column_widths": {"Name": 180, "Bad": -1},
            "sort_state": ["Name", False, True]})
        self.assertEqual(1.0, saved["text_scale"])
        self.assertFalse(saved["recursive"])
        self.assertEqual("gpt-6-luna", saved["model"])
        self.assertEqual({"Name": 180}, saved["column_widths"])
        self.assertEqual(["Name", False, True], saved["sort_state"])
        self.assertTrue(saved["remember_api_key"])
        self.assertNotIn("sensitive", self.path.read_text())
        self.assertEqual(saved, self.store.load())

    def test_legacy_display_preferences_cannot_change_fixed_ui_defaults(self):
        normalized=prefs.normalize_preferences(dict(text_scale=1.3,smooth_scrolling=False,reduce_motion=True))
        self.assertEqual(normalized['text_scale'],1.0)
        self.assertTrue(normalized['smooth_scrolling'])
        self.assertFalse(normalized['reduce_motion'])

    def test_invalid_values_use_defaults(self):
        saved = self.store.save({"text_scale": 9, "model": "", "last_folder": 123,
            "column_widths": [], "sort_state": {"key": "unrecognized"},
            "smooth_scrolling": None})
        self.assertEqual(prefs.DEFAULT_PREFERENCES, saved)

    def test_atomic_failure_keeps_previous_file_and_cleans_temporary_file(self):
        self.store.save({"model": "original"})
        before = self.path.read_bytes()
        with patch.object(prefs.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.store.save({"model": "replacement"})
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual([self.path], list(self.path.parent.iterdir()))


class SecureKeyTests(unittest.TestCase):
    def test_missing_keyring_has_explicit_error_and_no_plaintext_fallback(self):
        store = prefs.SecureApiKeyStore()
        with patch.object(prefs.importlib, "import_module", side_effect=ImportError("missing")):
            for action in (store.get, lambda: store.set("secret"), store.delete):
                with self.assertRaisesRegex(prefs.SecureStorageUnavailable, "secure|keyring|Keychain"):
                    action()

    def test_uses_only_os_keyring_operations(self):
        class MemoryKeyring:
            __module__ = "keyring.backends.macOS"
            value = None
            def get_keyring(self):
                return self
            def get_password(self, service, user):
                return self.value
            def set_password(self, service, user, key):
                self.value = key
            def delete_password(self, service, user):
                self.value = None
        backend = MemoryKeyring()
        store = prefs.SecureApiKeyStore()
        with patch.object(prefs.importlib, "import_module", return_value=backend):
            self.assertIsNone(store.get())
            store.set("test-secret")
            self.assertEqual("test-secret", store.get())
            store.delete()
            self.assertIsNone(store.get())

    def test_rejects_plaintext_null_and_chained_backends(self):
        for module in ("keyrings.alt.file", "keyring.backends.null", "keyring.backends.chainer"):
            backend_type = type("Keyring", (), {"__module__": module})
            backend = backend_type()
            backend.get_keyring = lambda: backend
            backend.get_password = lambda *args: "plaintext-secret"
            with self.subTest(module=module):
                with patch.object(prefs.importlib, "import_module", return_value=backend):
                    with self.assertRaises(prefs.SecureStorageUnavailable):
                        prefs.SecureApiKeyStore().get()


class SettingsWindowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = prefs.ctk.CTk()
        except prefs.tk.TclError as exc:
            self.skipTest(f"No graphical display: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.callback_errors = []
        self.root.report_callback_exception = lambda *args: self.callback_errors.append(args)
        self.addCleanup(lambda: self.assertEqual([], self.callback_errors))
        self.saves = []
        self.clears = []
        self.window = prefs.SettingsWindow(self.root, prefs.DEFAULT_PREFERENCES,
            "session-key", ["gpt-6-luna"], lambda draft, key: self.saves.append((draft, key)),
            on_clear_analysis=lambda: self.clears.append("analysis"))
        self.root.update()

    def test_general_and_connections_only_show_requested_controls(self):
        def labels(widget):
            result=[]
            if isinstance(widget,(prefs.ctk.CTkLabel,prefs.ctk.CTkButton,prefs.ctk.CTkSwitch)): result.append(widget.cget('text'))
            for child in widget.winfo_children(): result.extend(labels(child))
            return result
        general=' '.join(labels(self.window._pages['General']))
        connections=' '.join(labels(self.window._pages['Connections']))
        for removed in ('Text size','Smooth scrolling','Reduce motion'):
            self.assertNotIn(removed,general)
        self.assertNotIn('Drive',connections)
        self.assertNotIn('drive_folder_id',self.window.variables)

    def test_settings_window_wheel_scrolls_active_page_when_window_has_focus(self):
        self.window.show();self.window.geometry('640x480')
        self.window._select_page('Export');self.root.update()
        canvas=self.window.page._parent_canvas
        self.assertLess(canvas.yview()[1],1.0)
        before=canvas.yview()[0]
        self.window.event_generate('<MouseWheel>',delta=-10)
        self.root.update()
        self.assertGreater(canvas.yview()[0],before)
        self.window.event_generate('<MouseWheel>',delta=10)
        self.root.update()
        self.assertAlmostEqual(canvas.yview()[0],before,delta=.02)

    def test_wheel_from_child_scrolls_once_and_survives_reopening(self):
        for _ in range(3):
            self.window.show();self.window.geometry('640x480')
            self.window._select_page('Export');self.root.update()
            page=self.window.page;canvas=page._parent_canvas
            canvas.yview_moveto(0)
            before=canvas.yview()[0]
            for target in (page.winfo_children()[0],page._scrollbar._canvas):
                canvas.yview_moveto(0)
                before=canvas.yview()[0]
                target.event_generate('<MouseWheel>',delta=-120)
                self.root.update()
                extent=canvas.bbox('all')[3]
                self.assertAlmostEqual((canvas.yview()[0]-before)*extent,48,delta=.1)
            self.window.cancel()
        self.assertEqual(self.window._active_page,'Export')

    def test_cancel_reverts_draft_key_and_pending_clear(self):
        self.window.variables["recursive"].set(True)
        self.window.api_entry.delete(0, "end")
        self.window.api_entry.insert(0, "draft-key")
        self.window._queue_clear("analysis")
        self.assertEqual([], self.saves)
        self.assertEqual([], self.clears)
        self.window.cancel()
        self.assertFalse(self.window.get_draft()["recursive"])
        self.assertEqual("session-key", self.window.api_entry.get())
        self.assertEqual(set(), self.window._pending_clears)

    def test_save_commits_normalized_draft_and_queued_clear(self):
        self.window.variables["recursive"].set(True)
        self.window.show(current=dict(prefs.DEFAULT_PREFERENCES,drive_folder_id="ABC_123",recursive=True))
        self.window._queue_clear("analysis")
        self.window.save()
        self.assertTrue(self.saves[0][0]["recursive"])
        self.assertEqual("ABC_123", self.saves[0][0]["drive_folder_id"])
        self.assertEqual("session-key", self.saves[0][1])
        self.assertEqual(["analysis"], self.clears)
        self.window.cancel()
        self.assertTrue(self.window.get_draft()["recursive"])

    def test_busy_locks_only_connection_inputs_and_explains_why(self):
        self.window.show()
        self.window.set_busy(True)
        self.root.update()
        self.assertEqual("disabled", self.window.api_entry.cget("state"))
        self.assertEqual("disabled", self.window.model_menu.cget("state"))
        self.assertTrue(self.window.connection_note.winfo_ismapped())
        self.assertEqual("normal", self.window.save_button.cget("state"))
        self.window.variables["recursive"].set(True)
        self.window.save()
        self.assertTrue(self.saves[0][0]["recursive"])
        self.window.show()
        self.window.set_busy(False)
        self.root.update()
        self.assertEqual("normal", self.window.api_entry.cget("state"))
        self.assertFalse(self.window.connection_note.winfo_ismapped())

    def test_show_refreshes_from_latest_application_preferences(self):
        self.window.variables["recursive"].set(True)
        self.window.show(current={"model": "updated-model", "recursive": False}, api_key="new-key")
        self.assertFalse(self.window.get_draft()["recursive"])
        self.assertEqual("updated-model", self.window.model_menu.get())
        self.assertEqual("new-key", self.window.api_entry.get())

    def test_descriptions_wrap_at_fixed_size_and_minimum_window_size(self):
        prefs.ctk.set_widget_scaling(1.0)
        self.addCleanup(lambda: prefs.ctk.set_widget_scaling(1.0))
        self.window.show()
        self.window.geometry("640x480")
        for category, page in self.window._pages.items():
            self.window._select_page(category)
            self.root.update()
            for widget in page.winfo_children():
                if (isinstance(widget, prefs.ctk.CTkLabel) and len(widget.cget("text")) > 45
                        and widget.winfo_ismapped()):
                    self.assertLessEqual(widget._label.winfo_reqwidth(), widget.winfo_width(),
                                         f"Clipped description on {category}")
        self.assertTrue(self.window.save_button.winfo_ismapped())
        self.assertLessEqual(self.window.save_button.winfo_rootx() + self.window.save_button.winfo_width(),
                             self.window.winfo_rootx() + self.window.winfo_width())


if __name__ == "__main__":
    unittest.main()
