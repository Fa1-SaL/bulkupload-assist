"""Cloud Run boundary: only authenticated, bounded uploads reach the owner account."""
import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from upload_service.server import ServiceConfig, create_app, forward_to_bridge


class UploadServiceTests(unittest.TestCase):
    def setUp(self):
        self.code = "test-code-with-enough-entropy-for-tests"
        self.config = ServiceConfig(
            bridge_url="https://script.google.com/macros/s/test/exec",
            bridge_secret="bridge-test-secret",
            code_hashes={"user1": hashlib.sha256(self.code.encode()).hexdigest()},
            max_bytes=8,
        )
        self.upload = MagicMock(return_value="https://drive.google.com/file/d/abc/view")
        self.client = TestClient(create_app(self.config, upload_func=self.upload))

    def _send(self, code=None, body=b"pdf", name="r%C3%A9sum%C3%A9.pdf"):
        headers = {"X-Upload-Filename": name, "Content-Type": "application/octet-stream"}
        if code is not None:
            headers["Authorization"] = "Bearer " + code
        return self.client.post("/v1/uploads", headers=headers, content=body)

    def test_only_a_valid_user_code_can_upload(self):
        for code in (None, "bad-code", ""):
            with self.subTest(code=code):
                self.assertEqual(self._send(code).status_code, 401)
        self.upload.assert_not_called()
        response = self._send(self.code)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"url": "https://drive.google.com/file/d/abc/view"})
        self.assertEqual(self.upload.call_args.args[1], "résumé.pdf")
        self.assertEqual(self.upload.call_args.args[2].bridge_url,
                         "https://script.google.com/macros/s/test/exec")
        self.assertEqual(self.client.get("/v1/check").status_code, 401)
        self.assertEqual(self.client.get("/v1/check", headers={"Authorization": "Bearer " + self.code}).status_code, 200)

    def test_size_limit_and_unsafe_name_fail_before_upload(self):
        self.assertEqual(self._send(self.code, b"x" * 9).status_code, 413)
        self.assertEqual(self._send(self.code, b"x", "..%2Fsecret.pdf").status_code, 400)
        self.assertEqual(self._send(self.code, b"x", "malware.exe").status_code, 400)
        self.assertEqual(self._send(self.code, b"x", "resume.pdf%00.exe").status_code, 400)
        self.upload.assert_not_called()

    def test_temp_file_is_removed_after_success_and_failure(self):
        seen = []
        def upload(path, filename, config):
            seen.append((path, Path(path).read_bytes()))
            if len(seen) == 2:
                raise RuntimeError("sensitive google error")
            return "https://drive.google.com/file/d/abc/view"
        client = TestClient(create_app(self.config, upload_func=upload))
        headers = {"Authorization": "Bearer " + self.code,
                   "X-Upload-Filename": "resume.pdf", "Content-Type": "application/octet-stream"}
        self.assertEqual(client.post("/v1/uploads", headers=headers, content=b"one").status_code, 200)
        failure = client.post("/v1/uploads", headers=headers, content=b"two")
        self.assertEqual(failure.status_code, 502)
        self.assertNotIn("sensitive", failure.text)
        self.assertEqual([data for _, data in seen], [b"one", b"two"])
        self.assertTrue(all(not Path(path).exists() for path, _ in seen))

    def test_bridge_request_is_signed_and_contains_no_owner_token(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"status": "ok", "url": "https://drive.google.com/file/d/abc/view"}
        with tempfile.NamedTemporaryFile() as sample, \
             patch("upload_service.server.requests.post", return_value=response) as post:
            sample.write(b"test-pdf")
            sample.flush()
            link = forward_to_bridge(sample.name, "resume.pdf", self.config)
        self.assertEqual(link, response.json.return_value["url"])
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("refresh_token", json.dumps(payload))
        self.assertEqual(payload["name"], "resume.pdf")
        canonical = "\n".join(str(payload[key]) for key in ("timestamp", "nonce", "name", "mime", "data"))
        expected = hmac.new(self.config.bridge_secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(payload["signature"], expected)

    def test_bridge_failure_is_not_exposed_to_desktop(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"status": "error", "code": "secret-internal-detail"}
        with tempfile.NamedTemporaryFile() as sample, \
             patch("upload_service.server.requests.post", return_value=response):
            with self.assertRaises(RuntimeError):
                forward_to_bridge(sample.name, "resume.pdf", self.config)

    def test_configuration_requires_all_secrets_and_fixed_folder(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "APPS_SCRIPT_URL"):
                ServiceConfig.from_env()

    def test_server_factory_fails_closed_without_configuration(self):
        from upload_service.server import build_app
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "APPS_SCRIPT_URL"):
                build_app()

    def test_file_backed_secrets_accept_one_trailing_newline(self):
        values = {
            "APPS_SCRIPT_URL": "https://script.google.com/macros/s/test/exec\n",
            "BRIDGE_SHARED_SECRET": "a-secret-long-enough-for-the-owner-bridge\n",
            "UPLOAD_ACCESS_CODE_HASHES": json.dumps({"alice": "a" * 64}) + "\n",
        }
        with patch.dict("os.environ", values, clear=True):
            config = ServiceConfig.from_env()
        self.assertEqual(config.bridge_url, values["APPS_SCRIPT_URL"].rstrip("\n"))
        self.assertEqual(config.bridge_secret, values["BRIDGE_SHARED_SECRET"].rstrip("\n"))
