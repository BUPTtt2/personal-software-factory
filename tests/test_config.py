import json
import tempfile
import unittest
from pathlib import Path

from observer.config import ConfigError, load_projects


class ProjectConfigTests(unittest.TestCase):
    def write_registry(self, payload: dict) -> Path:
        directory = Path(tempfile.mkdtemp())
        registry = directory / "projects.yaml"
        registry.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return registry

    def test_load_projects_accepts_json_compatible_yaml(self):
        registry = self.write_registry({"projects": [{
            "id": "demo", "displayName": "Demo", "roots": ["/tmp/demo"],
            "gitRemotes": [], "enabled": True,
        }]})
        projects = load_projects(registry)
        self.assertEqual(projects[0].project_id, "demo")
        self.assertEqual(projects[0].roots, (Path("/tmp/demo").resolve(strict=False),))

    def test_load_projects_rejects_duplicate_ids(self):
        registry = self.write_registry({"projects": [
            {"id": "demo", "displayName": "One", "roots": ["/tmp/one"], "gitRemotes": [], "enabled": True},
            {"id": "demo", "displayName": "Two", "roots": ["/tmp/two"], "gitRemotes": [], "enabled": True},
        ]})
        with self.assertRaises(ConfigError):
            load_projects(registry)

    def test_load_projects_rejects_relative_roots(self):
        registry = self.write_registry({"projects": [{
            "id": "demo", "displayName": "Demo", "roots": ["../demo"],
            "gitRemotes": [], "enabled": True,
        }]})
        with self.assertRaises(ConfigError):
            load_projects(registry)


if __name__ == "__main__":
    unittest.main()
