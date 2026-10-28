"""Owner-issued codes match the per-user key derived by Apps Script."""
import hashlib
import hmac
import unittest

from owner_upload.provision import derive_access_code


class OwnerCodeTests(unittest.TestCase):
    def test_code_is_unique_per_user_and_matches_hmac_contract(self):
        secret = 'example-owner-secret-with-adequate-entropy'
        code = derive_access_code('owner', secret)
        key = hmac.new(secret.encode(), b'bulk-upload-code-v1:owner', hashlib.sha256).hexdigest()
        self.assertEqual(code, 'owner:' + key)
        self.assertNotEqual(code, derive_access_code('member-1', secret))

    def test_invalid_user_label_is_rejected(self):
        for label in ('', '../owner', 'name with spaces', 'x' * 41):
            with self.subTest(label=label), self.assertRaises(ValueError):
                derive_access_code(label, 'example-owner-secret-with-adequate-entropy')
