import unittest

from observer.redact import contains_sensitive_text, redact_text, sanitize_url


class RedactionTests(unittest.TestCase):
    def test_redacts_bearer_and_assignments(self):
        self.assertEqual(
            redact_text("Authorization: Bearer abc.def.ghi").text,
            "Authorization: Bearer [REDACTED]",
        )
        self.assertNotIn("sk-live-secret", redact_text("api_key=sk-live-secret").text)
        self.assertNotIn("hunter2", redact_text("password: hunter2").text)

    def test_redacts_private_keys(self):
        value = "-----BEGIN " + "PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
        self.assertNotIn("BEGIN PRIVATE KEY", redact_text(value).text)

    def test_truncates_without_exceeding_limit(self):
        self.assertLessEqual(len(redact_text("x" * 1000, max_chars=80).text), 80)

    def test_preserves_safe_chinese_text(self):
        self.assertEqual(redact_text("普通产品问题").text, "普通产品问题")

    def test_sanitizes_sensitive_url_values(self):
        value = sanitize_url("https://example.test/a?token=secret&view=1#frag")
        self.assertIn("token=", value)
        self.assertNotIn("secret", value)
        self.assertIn("view=1", value)
        self.assertNotIn("frag", value)

    def test_redacts_common_standalone_tokens_and_url_userinfo(self):
        samples = (
            "glpat-abcdefghijklmnop", "sk_live_abcdefghijklmnop",
            "AIzaSyA12345678901234567890", "npm_abcdefghijklmnopqrstuvwxyz",
            "hf_abcdefghijklmnopqrstuvwxyz", "https://user:password@example.test/path",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertNotIn(sample, redact_text(sample).text)

    def test_git_sha_is_not_misclassified_as_secret(self):
        self.assertFalse(contains_sensitive_text("a" * 40))
        self.assertTrue(contains_sensitive_text("AbCdEfGhIjKlMnOpQrStUvWxYz0123456789ABCD"))


if __name__ == "__main__":
    unittest.main()
