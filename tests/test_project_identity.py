import subprocess
import shutil
import tempfile
import unittest
from pathlib import Path

from observer.config import ProjectConfig
from observer.project_identity import identify_project


GIT = shutil.which("git") or "git"


def project(project_id: str, root: Path, enabled: bool = True) -> ProjectConfig:
    return ProjectConfig(project_id, project_id, (root.resolve(),), (), enabled)


class ProjectIdentityTests(unittest.TestCase):
    def test_prefers_longest_registered_root(self):
        root = Path(tempfile.mkdtemp())
        nested = root / "nested"
        child = nested / "src"
        child.mkdir(parents=True)
        identity = identify_project(child, (project("outer", root), project("nested", nested)), git_executable=GIT)
        self.assertEqual(identity.project_id, "nested")
        self.assertEqual(identity.matched_by, "root")

    def test_ignores_disabled_and_unregistered_projects(self):
        root = Path(tempfile.mkdtemp())
        self.assertIsNone(identify_project(root, (project("off", root, False),), git_executable=GIT))

    def test_maps_linked_worktree_to_registered_repository(self):
        area = Path(tempfile.mkdtemp())
        repo = area / "repo"
        worktree = area / "linked"
        subprocess.run([GIT, "init", "-b", "main", str(repo)], check=True, capture_output=True)
        subprocess.run([GIT, "-C", str(repo), "config", "user.name", "Observer Test"], check=True)
        subprocess.run([GIT, "-C", str(repo), "config", "user.email", "observer@test.invalid"], check=True)
        (repo / "seed.txt").write_text("seed", encoding="utf-8")
        subprocess.run([GIT, "-C", str(repo), "add", "seed.txt"], check=True)
        subprocess.run([GIT, "-C", str(repo), "commit", "-m", "seed"], check=True, capture_output=True)
        subprocess.run([GIT, "-C", str(repo), "worktree", "add", "-b", "linked", str(worktree)], check=True, capture_output=True)
        identity = identify_project(worktree, (project("demo", repo),), git_executable=GIT)
        self.assertEqual(identity.project_id, "demo")
        self.assertEqual(identity.matched_by, "git_common_dir")


if __name__ == "__main__":
    unittest.main()
