import unittest

from observer.verification import classify_command, classify_verification


class VerificationTests(unittest.TestCase):
    def test_public_argv_classifier_accepts_only_verification_commands(self):
        self.assertEqual(
            classify_command(["/usr/bin/python3", "-m", "unittest", "tests.test_redact"]),
            ("test", "python_unittest"),
        )
        self.assertIsNone(classify_command(["git", "push"]))

    def test_classifies_allowlisted_test_commands(self):
        observed = classify_verification(
            "Bash", {"command": "python3 -m unittest tests.test_redact -v"}, {"exit_code": 0},
        )
        self.assertEqual((observed.kind, observed.command_class, observed.status), ("test", "python_unittest", "passed"))
        failed = classify_verification("Bash", {"command": "npm test"}, {"result": {"exitCode": 1}})
        self.assertEqual((failed.exit_code, failed.status), (1, "failed"))

    def test_unknown_exit_shape_never_passes(self):
        observed = classify_verification("Bash", {"command": "npm run build"}, {"output": "done"})
        self.assertEqual(observed.status, "unknown")

    def test_rejects_non_verification_and_compound_commands(self):
        for command in (
            "echo test", "curl https://example.test", "git push", "rm file",
            "npm test && git push", "npm test; echo done", "mvn deploy test",
            "gradle publish test",
        ):
            with self.subTest(command=command):
                self.assertIsNone(classify_verification("Bash", {"command": command}, {"exit_code": 0}))
        self.assertIsNone(classify_verification("apply_patch", {"command": "npm test"}, {"exit_code": 0}))


if __name__ == "__main__":
    unittest.main()
