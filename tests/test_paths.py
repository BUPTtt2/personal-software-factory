import os
import unittest
from pathlib import Path
from unittest.mock import patch

from observer.paths import resolve_runtime_paths


class RuntimePathsTests(unittest.TestCase):
    def test_explicit_state_root_wins_over_environment(self):
        with patch.dict(os.environ, {"SOFTWARE_FACTORY_HOME": "/tmp/from-env"}):
            paths = resolve_runtime_paths(Path("/plugin"), Path("/tmp/explicit"))
        self.assertEqual(paths.state_root, Path("/tmp/explicit"))
        self.assertEqual(paths.registry_path, Path("/tmp/explicit/config/projects.json"))

    def test_environment_state_root_wins_over_platform_default(self):
        with patch.dict(os.environ, {"SOFTWARE_FACTORY_HOME": "/tmp/from-env"}):
            paths = resolve_runtime_paths(Path("/plugin"))
        self.assertEqual(paths.state_root, Path("/tmp/from-env"))

    def test_macos_default_state_root_is_not_inside_plugin(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("observer.paths.platform.system", return_value="Darwin"),
            patch("observer.paths.Path.home", return_value=Path("/home/demo")),
        ):
            paths = resolve_runtime_paths(Path("/plugin"))
        self.assertEqual(
            paths.state_root,
            Path("/home/demo/Library/Application Support/PersonalSoftwareFactory"),
        )
        self.assertFalse(paths.state_root.is_relative_to(paths.code_root))

    def test_registry_override_is_resolved_independently(self):
        paths = resolve_runtime_paths(
            Path("/plugin"), Path("/state"), Path("/registries/projects.json"),
        )
        self.assertEqual(paths.registry_path, Path("/registries/projects.json"))
        self.assertEqual(paths.db_path, Path("/state/data/factory.sqlite"))
        self.assertEqual(paths.buffer_dir, Path("/state/buffer"))


if __name__ == "__main__":
    unittest.main()
