import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ConsoleServiceScriptTests(unittest.TestCase):
    def setUp(self):
        self.area = Path(tempfile.mkdtemp())
        self.home = self.area / "home"
        self.home.mkdir()
        self.fake_launchctl = self.area / "launchctl"
        self.launchctl_log = self.area / "launchctl.log"
        self.fake_launchctl.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CONSOLE_SERVICE_LAUNCHCTL_LOG\"\n"
            "case \"$1\" in\n"
            "  print) [ -f \"$CONSOLE_SERVICE_LOADED\" ] || exit 1; echo 'pid = 99999';;\n"
            "  bootstrap) if [ -f \"${CONSOLE_SERVICE_FAIL_ONCE:-/missing}\" ]; then rm -f \"$CONSOLE_SERVICE_FAIL_ONCE\"; exit 1; fi; touch \"$CONSOLE_SERVICE_LOADED\";;\n"
            "  bootout) rm -f \"$CONSOLE_SERVICE_LOADED\";;\n"
            "esac\nexit 0\n",
            encoding="utf-8",
        )
        self.fake_launchctl.chmod(0o755)
        self.env = os.environ | {
            "HOME": str(self.home),
            "CONSOLE_SERVICE_HOME": str(self.home),
            "CONSOLE_SERVICE_ROOT": str(ROOT),
            "CONSOLE_SERVICE_LAUNCHCTL": str(self.fake_launchctl),
            "CONSOLE_SERVICE_LAUNCHCTL_LOG": str(self.launchctl_log),
            "CONSOLE_SERVICE_PYTHON": sys.executable,
            "CONSOLE_SERVICE_CODEX": "/usr/bin/true",
            "CONSOLE_SERVICE_PORT": "18765",
            "CONSOLE_SERVICE_LOADED": str(self.area / "loaded"),
        }

    def run_script(self, name: str, *args: str, check: bool = True):
        return subprocess.run(
            [str(ROOT / "scripts" / name), *args],
            env=self.env,
            text=True,
            capture_output=True,
            check=check,
        )

    @property
    def plist(self):
        return self.home / "Library/LaunchAgents/dev.personal-software-factory.observer.plist"

    def test_preview_is_read_only_and_apply_writes_only_owned_service(self):
        preview = self.run_script("install-console-service.sh", "--preview")
        self.assertIn(str(self.plist), preview.stdout)
        self.assertFalse(self.plist.exists())

        self.run_script("install-console-service.sh", "--apply")
        text = self.plist.read_text(encoding="utf-8")
        self.assertIn("dev.personal-software-factory.observer", text)
        self.assertIn("<key>KeepAlive</key>", text)
        self.assertIn("127.0.0.1", text)
        self.assertIn(str(ROOT), text)
        launchctl = self.launchctl_log.read_text(encoding="utf-8")
        self.assertIn("bootstrap", launchctl)
        self.assertNotIn("hooks.json", text)

    def test_apply_refuses_port_owned_by_another_process(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.env["CONSOLE_SERVICE_PORT"] = str(listener.getsockname()[1])
        try:
            result = self.run_script("install-console-service.sh", "--apply", check=False)
        finally:
            listener.close()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already in use", result.stderr)
        self.assertFalse(self.plist.exists())

    def test_uninstall_preview_and_apply_preserve_observer_data(self):
        data = ROOT / "data/factory.sqlite"
        before = data.exists()
        self.run_script("install-console-service.sh", "--apply")
        preview = self.run_script("uninstall-console-service.sh", "--preview")
        self.assertTrue(self.plist.exists())
        self.assertIn(str(self.plist), preview.stdout)
        self.run_script("uninstall-console-service.sh", "--apply")
        self.assertFalse(self.plist.exists())
        self.assertEqual(data.exists(), before)

    def test_failed_upgrade_restores_existing_plist_and_service(self):
        self.run_script("install-console-service.sh", "--apply")
        before = self.plist.read_bytes()
        fail_once = self.area / "fail-once"
        fail_once.touch()
        self.env["CONSOLE_SERVICE_FAIL_ONCE"] = str(fail_once)
        result = self.run_script("install-console-service.sh", "--apply", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.plist.read_bytes(), before)
        self.assertTrue((self.area / "loaded").exists())


if __name__ == "__main__":
    unittest.main()
