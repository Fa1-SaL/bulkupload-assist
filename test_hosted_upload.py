"""Desktop uploads use the hosted owner service, never local Google OAuth."""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import drive_uploader


class HostedUploadClientTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = os.path.join(temp.name, "résumé.pdf")
        with open(self.path, "wb") as file:
            file.write(b"test-pdf")
        self.service = drive_uploader.HostedDriveService(
            "https://upload.example.com", "user-code", MagicMock())

    def test_local_google_token_is_never_used(self):
        with patch.object(drive_uploader, "UPLOAD_SERVICE_URL", "https://upload.example.com"), \
             patch("app_preferences.SecureApiKeyStore") as store:
            store.return_value.get.return_value = "user-code"
            self.assertEqual(drive_uploader.get_drive_credentials(), "user-code")
        store.assert_called_once_with(service_name="BulkUploadAssist", username="upload_access_code")

    def test_upload_streams_file_with_code_and_encoded_filename(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"url": "https://drive.google.com/file/d/abc/view"}
        self.service.session.post.return_value = response
        link = drive_uploader._upload_single_file(self.service, self.path)
        self.assertEqual(link, response.json.return_value["url"])
        args, kwargs = self.service.session.post.call_args
        self.assertEqual(args[0], "https://upload.example.com/v1/uploads")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer user-code")
        self.assertEqual(kwargs["headers"]["X-Upload-Filename"], "r%C3%A9sum%C3%A9.pdf")
        self.assertEqual(b"".join(kwargs["data"]), b"test-pdf")

    def test_code_rejection_is_clear_and_not_retried(self):
        self.service.session.post.return_value = MagicMock(status_code=401)
        with self.assertRaisesRegex(RuntimeError, "access code"):
            drive_uploader._upload_single_file(self.service, self.path)
        self.assertEqual(self.service.session.post.call_count, 1)

    def test_other_folder_cannot_be_requested(self):
        with self.assertRaisesRegex(ValueError, "fixed"):
            drive_uploader._upload_single_file(self.service, self.path, parent_id="other-folder")
        self.service.session.post.assert_not_called()

    def test_transient_service_error_retries_then_succeeds(self):
        failure = MagicMock(status_code=502)
        success = MagicMock(status_code=200)
        success.json.return_value = {"url": "https://drive.google.com/file/d/abc/view"}
        self.service.session.post.side_effect = [failure, success]
        with patch.object(drive_uploader.time, "sleep"), \
             patch.object(drive_uploader, "log_upload_error"):
            self.assertEqual(drive_uploader._upload_single_file(self.service, self.path),
                             success.json.return_value["url"])
        self.assertEqual(self.service.session.post.call_count, 2)

    def test_cache_key_separates_hosted_and_old_direct_links(self):
        key = drive_uploader.hosted_cache_key("a" * 32)
        self.assertNotEqual(key, "a" * 32)
        self.assertIn(drive_uploader.DRIVE_FOLDER_ID, key)
