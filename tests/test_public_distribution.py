import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PublicDistributionTests(unittest.TestCase):
    def test_package_metadata_exposes_plugin_cli_and_python_floor(self):
        value = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project = value["project"]
        self.assertEqual(project["name"], "personal-software-factory")
        self.assertEqual(project["version"], "0.1.0")
        self.assertEqual(project["requires-python"], ">=3.11")
        self.assertEqual(project["license"], "Apache-2.0")
        self.assertNotIn(
            "License :: OSI Approved :: Apache Software License",
            project["classifiers"],
        )
        self.assertEqual(project["scripts"]["personal-software-factory"], "observer.cli:main")

    def test_required_open_source_documents_exist_and_readme_is_actionable(self):
        for name in ("README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md", "CHANGELOG.md"):
            self.assertGreater((ROOT / name).stat().st_size, 100, name)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for command in (
            "personal-software-factory init-db",
            "personal-software-factory serve",
            "codex plugin add personal-software-factory@personal",
        ):
            self.assertIn(command, readme)
        self.assertIn("does not modify", readme)

    def test_build_outputs_are_ignored(self):
        patterns = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        for pattern in ("build/", "dist/", "*.egg-info/"):
            self.assertIn(pattern, patterns)

    def test_privacy_scanner_rejects_private_paths_tokens_and_runtime_files(self):
        fixture = Path(tempfile.mkdtemp())
        (fixture / "safe.txt").write_text("public documentation", encoding="utf-8")
        safe = self.run_scan(fixture)
        self.assertEqual(safe.returncode, 0, safe.stdout + safe.stderr)

        private_home = "/" + "Users" + "/example/private"
        (fixture / "unsafe.txt").write_text(
            private_home + "\n" + "sk-" + "live-example-secret-value",
            encoding="utf-8",
        )
        (fixture / "runtime.sqlite").write_bytes(b"sqlite")
        unsafe = self.run_scan(fixture)
        self.assertNotEqual(unsafe.returncode, 0)
        report = json.loads(unsafe.stdout)
        self.assertEqual(report["status"], "failed")
        self.assertGreaterEqual(report["findingCount"], 3)

    def test_release_privacy_scan_rejects_present_ignored_prefix(self):
        fixture = Path(tempfile.mkdtemp())
        (fixture / ".publicignore").write_text("private-notes/\n", encoding="utf-8")
        (fixture / "private-notes").mkdir()
        (fixture / "private-notes/note.md").write_text("hidden", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/privacy-scan.py"), str(fixture), "--release"],
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ignored_prefix_present", result.stdout)

    def test_public_tree_has_no_personal_runtime_defaults(self):
        result = self.run_scan(ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def run_scan(self, target):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts/privacy-scan.py"), str(target)],
            text=True, capture_output=True, check=False,
        )


if __name__ == "__main__":
    unittest.main()
