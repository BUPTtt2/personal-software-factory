import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


class HookInstallationTests(unittest.TestCase):
    def setUp(self):
        self.area = Path(tempfile.mkdtemp())
        self.home = self.area / "codex-home"
        self.home.mkdir()
        self.root = self.area / "observer-root"
        self.state_root = self.area / "state"
        self.root.mkdir()
        (self.root / "scripts").mkdir()
        (self.root / "observer").mkdir()
        (self.root / "hooks").mkdir()
        (self.root / "scripts/observer-hook.py").write_text("# fixture", encoding="utf-8")
        (self.root / "observer/hook_install.py").write_bytes((ROOT / "observer/hook_install.py").read_bytes())
        (self.root / "hooks/codex-observer-hooks.json").write_bytes((ROOT / "hooks/codex-observer-hooks.json").read_bytes())
        self.env = os.environ | {
            "CODEX_OBSERVER_TEST_HOME": str(self.home),
            "CODEX_OBSERVER_ROOT": str(self.root),
            "CODEX_OBSERVER_PYTHON": PYTHON,
            "SOFTWARE_FACTORY_HOME": str(self.state_root),
        }

    def run_script(self, name: str, *args: str, check: bool = True):
        return subprocess.run(
            [str(ROOT / "scripts" / name), *args], env=self.env,
            text=True, capture_output=True, check=check,
        )

    def test_preview_does_not_write(self):
        result = self.run_script("install-observer.sh", "--preview")
        self.assertIn(str(self.home / "hooks.json"), result.stdout)
        self.assertFalse((self.home / "hooks.json").exists())

    def test_apply_is_idempotent_and_preserves_unrelated_hooks(self):
        existing = {"description": "existing", "hooks": {"Stop": [{"hooks": [{
            "type": "command", "command": "/bin/echo existing", "timeout": 2,
        }]}]}}
        (self.home / "hooks.json").write_text(json.dumps(existing), encoding="utf-8")
        self.run_script("install-observer.sh", "--apply")
        self.run_script("install-observer.sh", "--apply")
        installed = json.loads((self.home / "hooks.json").read_text(encoding="utf-8"))
        commands = [handler["command"] for group in installed["hooks"]["Stop"] for handler in group["hooks"]]
        self.assertIn("/bin/echo existing", commands)
        observer_commands = [item for item in commands if "observer-hook.py" in item]
        self.assertEqual(len(observer_commands), 1)
        self.assertIn(PYTHON, observer_commands[0])
        self.assertIn(str(self.root), observer_commands[0])
        self.assertIn(str(self.state_root), observer_commands[0])
        self.assertTrue(list((self.state_root / "backups").glob("hooks.json.*.bak")))

    def test_uninstall_removes_only_observer_and_preserves_data(self):
        (self.state_root / "data").mkdir(parents=True)
        (self.state_root / "data/factory.sqlite").write_bytes(b"evidence")
        self.run_script("install-observer.sh", "--apply")
        self.run_script("uninstall-observer.sh", "--apply")
        installed = json.loads((self.home / "hooks.json").read_text(encoding="utf-8"))
        text = json.dumps(installed)
        self.assertNotIn("observer-hook.py", text)
        self.assertTrue((self.state_root / "data/factory.sqlite").exists())

    def test_malformed_existing_json_aborts_without_overwrite(self):
        target = self.home / "hooks.json"
        target.write_text("{broken", encoding="utf-8")
        result = self.run_script("install-observer.sh", "--apply", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "{broken")

    def test_partial_purge_confirmation_does_not_mutate_hooks(self):
        self.run_script("install-observer.sh", "--apply")
        target = self.home / "hooks.json"
        before = target.read_bytes()
        result = self.run_script("uninstall-observer.sh", "--apply", "--purge-data", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
