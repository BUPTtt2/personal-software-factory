import subprocess
import shutil
import tempfile
import unittest
from pathlib import Path

from observer.git_snapshot import capture_git_snapshot


GIT = shutil.which("git") or "git"


class GitSnapshotTests(unittest.TestCase):
    def make_repo(self) -> Path:
        repo = Path(tempfile.mkdtemp()) / "repo"
        subprocess.run([GIT, "init", "-b", "main", str(repo)], check=True, capture_output=True)
        subprocess.run([GIT, "-C", str(repo), "config", "user.name", "Observer Test"], check=True)
        subprocess.run([GIT, "-C", str(repo), "config", "user.email", "observer@test.invalid"], check=True)
        (repo / "tracked.txt").write_text("original", encoding="utf-8")
        subprocess.run([GIT, "-C", str(repo), "add", "tracked.txt"], check=True)
        subprocess.run([GIT, "-C", str(repo), "commit", "-m", "seed"], check=True, capture_output=True)
        return repo

    def test_clean_and_dirty_snapshots_contain_paths_not_contents(self):
        repo = self.make_repo()
        clean = capture_git_snapshot(repo, git_executable=GIT)
        self.assertTrue(clean.available)
        self.assertFalse(clean.dirty)
        self.assertEqual(clean.branch, "main")
        (repo / "tracked.txt").write_text("highly-sensitive-content", encoding="utf-8")
        (repo / "new.txt").write_text("new-secret-content", encoding="utf-8")
        dirty = capture_git_snapshot(repo, git_executable=GIT)
        self.assertTrue(dirty.dirty)
        self.assertEqual(dirty.changed_paths, ("new.txt", "tracked.txt"))
        self.assertNotIn("highly-sensitive-content", repr(dirty))

    def test_non_git_directory_is_safe_unavailable(self):
        snapshot = capture_git_snapshot(Path(tempfile.mkdtemp()), git_executable=GIT)
        self.assertFalse(snapshot.available)
        self.assertEqual(snapshot.error_code, "not_git")

    def test_detached_head_has_no_branch(self):
        repo = self.make_repo()
        subprocess.run([GIT, "-C", str(repo), "checkout", "--detach"], check=True, capture_output=True)
        self.assertIsNone(capture_git_snapshot(repo, git_executable=GIT).branch)


if __name__ == "__main__":
    unittest.main()
