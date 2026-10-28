"""Desktop uploads call the owner's Apps Script, without local Google OAuth."""
import base64
import hashlib
import hmac
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import drive_uploader


class OwnerScriptClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = os.path.join(temporary.name, "résumé.pdf")
        with open(self.path, "wb") as file:
            file.write(b"test-pdf")
        self.code = "owner:" + "a" * 64
        self.service = drive_uploader.OwnerScriptService(
            "https://script.google.com/macros/s/test/exec", self.code, MagicMock())

    def test_local_google_token_is_never_used(self):
        with patch.object(drive_uploader, "UPLOAD_SERVICE_URL", self.service.url), \
             patch("app_preferences.SecureApiKeyStore") as store:
            store.return_value.get.return_value = self.code
            self.assertEqual(drive_uploader.get_drive_credentials(), self.code)
        store.assert_called_once_with(service_name="BulkUploadAssist", username="upload_access_code")

    def test_upload_posts_signed_file_without_exposing_the_code(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"status": "ok", "url": "https://drive.google.com/file/d/abc/view"}
        self.service.session.post.return_value = response
        link = drive_uploader._upload_single_file(self.service, self.path)
        self.assertEqual(link, response.json.return_value["url"])
        args, kwargs = self.service.session.post.call_args
        self.assertEqual(args[0], self.service.url)
        payload = kwargs["json"]
        self.assertEqual(payload["action"], "upload")
        self.assertEqual(payload["user"], "owner")
        self.assertEqual(payload["name"], "résumé.pdf")
        self.assertEqual(base64.b64decode(payload["data"]), b"test-pdf")
        self.assertNotIn(self.code, str(payload))
        canonical = "\n".join(str(payload[key]) for key in
                              ("action", "timestamp", "nonce", "user", "name", "mime", "data"))
        expected = hmac.new(("a" * 64).encode(), canonical.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(payload["signature"], expected)

    def test_connection_check_is_signed(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"status": "ready"}
        self.service.session.post.return_value = response
        self.assertTrue(drive_uploader.check_upload_access(self.service))
        payload = self.service.session.post.call_args.kwargs["json"]
        self.assertEqual(payload["action"], "check")
        self.assertNotIn("data", payload)

    def test_code_rejection_is_clear_and_not_retried(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"status": "error", "code": "unauthorized"}
        self.service.session.post.return_value = response
        with patch.object(drive_uploader, "log_upload_error"):
            with self.assertRaisesRegex(RuntimeError, "access code"):
                drive_uploader._upload_single_file(self.service, self.path)
        self.assertEqual(self.service.session.post.call_count, 1)

    def test_invalid_access_code_is_rejected_locally(self):
        with self.assertRaisesRegex(ValueError, "access code"):
            drive_uploader.parse_access_code("not-a-code")

    def test_other_folder_cannot_be_requested(self):
        with self.assertRaisesRegex(ValueError, "fixed"):
            drive_uploader._upload_single_file(self.service, self.path, parent_id="other-folder")
        self.service.session.post.assert_not_called()

    def test_transient_script_error_retries_then_succeeds(self):
        failure = MagicMock(status_code=200)
        failure.json.return_value = {"status": "error", "code": "busy"}
        success = MagicMock(status_code=200)
        success.json.return_value = {"status": "ok", "url": "https://drive.google.com/file/d/abc/view"}
        self.service.session.post.side_effect = [failure, success]
        with patch.object(drive_uploader.time, "sleep"), \
             patch.object(drive_uploader, "log_upload_error"):
            self.assertEqual(drive_uploader._upload_single_file(self.service, self.path),
                             success.json.return_value["url"])
        self.assertEqual(self.service.session.post.call_count, 2)

    def test_cache_key_separates_script_and_old_direct_links(self):
        key = drive_uploader.hosted_cache_key("a" * 32)
        self.assertNotEqual(key, "a" * 32)
        self.assertIn(drive_uploader.DRIVE_FOLDER_ID, key)
