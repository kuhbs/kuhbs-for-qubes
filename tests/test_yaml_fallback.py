# Purpose: Regression tests for strict YAML-backed config loading
# Scope: Uses temp dirs only; PyYAML is a required runtime dependency
from pathlib import Path
import tempfile
import unittest

from kuhbs.validation import ConfigValidationError, validate_startup_config


class ConfigYamlTests(unittest.TestCase):
    def test_startup_validation_reports_missing_nested_defaults_keys(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "defaults.yml"
            path.write_text("""schema: 1
paths:
  config: ~/.kuhbs
repos: {}
backup: {}
logging: {}
terminal: {}
prefs: {}
services: {}
features: {}
""")
            with self.assertRaisesRegex(ConfigValidationError, "paths.kuhbs"):
                validate_startup_config(path)

    def test_startup_validation_rejects_duplicate_yaml_keys(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "defaults.yml"
            path.write_text("""schema: 1
schema: 1
""", encoding="utf-8")

            with self.assertRaisesRegex(ConfigValidationError, "duplicate YAML key: schema"):
                validate_startup_config(path)


if __name__ == "__main__":
    unittest.main()
