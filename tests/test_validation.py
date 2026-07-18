# Purpose: Startup config validation regression tests
# Scope: Validates YAML files in temp dirs without touching Qubes state
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml
from jsonschema import Draft202012Validator

from kuhbs.config import load_defaults
from kuhbs.validation import ConfigValidationError, _schema_issues, inspect_startup_config, validate_kuhb_file, validate_startup_config


ROOT = Path(__file__).resolve().parents[1]


def write_defaults(root: Path) -> Path:
    defaults = root / "defaults.yml"
    data = load_defaults(ROOT / "defaults.yml")
    data["paths"]["config"] = f"{root}/config"
    data["paths"]["kuhbs"] = f"{root}/config/my-kuhbs"
    data["paths"]["scripts"] = f"{root}/config/work/scripts"
    data["paths"]["setup_scripts"] = f"{root}/usr/share/kuhbs/setup-scripts"
    data["paths"]["user_setup_scripts"] = f"{root}/config/setup-scripts"
    data["paths"]["desktop_applications"] = f"{root}/applications"
    data["terminal"]["args"] = []
    data["terminal"]["fallback"]["args"] = []
    defaults.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return defaults


def valid_definition(kuhb_id: str = "bad") -> dict:
    # Start regressions from one complete raw app definition so each test changes only its target field
    return {
        "id": kuhb_id,
        "name": "Validation test",
        "description": "Validation regression fixture",
        "icon": "icon.svg",
        "order": 500,
        "type": "app",
        "template": "debian-13-minimal",
        "kuhs": {
            "app": {
                "instances": [
                    {
                        "id": "default",
                        "prefs": {"label": "green"},
                    },
                ],
            },
        },
    }


def write_definition(root: Path, data: dict) -> Path:
    # Put the definition under startup discovery and provide the icon referenced by the fixture
    definition = root / "config/my-kuhbs" / data["id"] / "kuhb.yml"
    definition.parent.mkdir(parents=True, exist_ok=True)
    icon = definition.parent / data["icon"]
    icon.parent.mkdir(parents=True, exist_ok=True)
    icon.write_text("<svg/>", encoding="utf-8")
    launcher_dir = definition.parent / "launcher-icons"
    for kind, kind_config in data.get("kuhs", {}).items():
        configs = [kind_config] if kind == "tpl" else kind_config.get("instances", [])
        for config in configs:
            for launcher in config.get("launchers", []):
                launcher_dir.mkdir(parents=True, exist_ok=True)
                (launcher_dir / f"{launcher['id']}.svg").write_text("<svg/>", encoding="utf-8")
    definition.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return definition


class ValidationTests(unittest.TestCase):
    def test_logging_defaults_configure_terminal_output_only(self):
        defaults = load_defaults(ROOT / "defaults.yml")

        self.assertEqual(set(defaults["logging"]), {"spacing", "colors"})

    def test_no_local_kuhbs_is_valid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)

            validated = validate_startup_config(defaults)

            self.assertEqual(validated.defaults_path, defaults)
            self.assertEqual(validated.kuhb_definitions, ())

    def test_active_kuhb_malformed_lifecycle_state_fails_startup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            write_definition(root, valid_definition("browse"))
            state_file = root / "config/states/browse/create"
            state_file.parent.mkdir(parents=True)
            state_file.write_text("asdf\n", encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn(
                f'{state_file}: invalid lifecycle state "asdf"; expected start, completed, or failed',
                str(cm.exception),
            )

    def test_active_kuhb_accepts_every_lifecycle_state_word(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            write_definition(root, valid_definition("browse"))
            state_dir = root / "config/states/browse"
            state_dir.mkdir(parents=True)
            for action, state in zip(
                ("create", "backup", "restore", "remove"),
                ("start", "completed", "failed", "completed"),
            ):
                (state_dir / action).write_text(f"{state}\n", encoding="utf-8")

            validate_startup_config(defaults)

    def test_inactive_kuhb_state_directory_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            write_definition(root, valid_definition("browse"))
            state_file = root / "config/states/old-removed/create"
            state_file.parent.mkdir(parents=True)
            state_file.write_text("asdf\n", encoding="utf-8")

            validate_startup_config(defaults)

    def test_unknown_and_missing_active_state_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            write_definition(root, valid_definition("browse"))
            state_file = root / "config/states/browse/foobar"
            state_file.parent.mkdir(parents=True)
            state_file.write_text("anything\n", encoding="utf-8")

            validate_startup_config(defaults)

    def test_active_kuhb_directory_without_definition_is_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            (root / "config/my-kuhbs/broken").mkdir(parents=True)

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("kuhb.yml must be a real file", str(cm.exception))

    def test_repo_commit_selection_count_must_be_positive_integer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = load_defaults(defaults)
            data["repos"]["commit_selection_count"] = 0
            defaults.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("repos.commit_selection_count must be a positive integer", str(cm.exception))

    def test_all_local_kuhbs_are_validated_before_startup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            bad = root / "config/my-kuhbs/bad/kuhb.yml"
            bad.parent.mkdir(parents=True)
            (bad.parent / "icon.svg").write_text("<svg/>", encoding="utf-8")
            bad.write_text("""id: bad
name: Bad
description: Missing app instance id
icon: icon.svg
order: 500
type: app
template: debian-13-minimal
kuhs:
  tpl: {}
  app:
    instances:
      - prefs:
          label: green
""", encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn(str(bad), str(cm.exception))
            self.assertIn("kuhs.app.instances[0].id is required", str(cm.exception))

    def test_kuhb_id_must_match_directory_or_link_name(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            bad = root / "config/my-kuhbs/wrong-name/kuhb.yml"
            bad.parent.mkdir(parents=True)
            (bad.parent / "icon.svg").write_text("<svg/>", encoding="utf-8")
            bad.write_text("""id: real-name
name: Bad
description: Directory name must match id
icon: icon.svg
order: 500
type: app
template: debian-13-minimal
kuhs:
  app:
    instances:
      - id: default
        prefs:
          label: green
""", encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("id must match KUHB directory/link name wrong-name: got real-name", str(cm.exception))

    def test_check_qubes_reads_xml_instead_of_running_qvm_check(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            write_definition(root, valid_definition("good"))
            qubes_xml = root / "qubes.xml"
            qubes_xml.write_text(
                '<qubes><domains><domain class="TemplateVM"><properties>'
                '<property name="name">other-template</property>'
                '</properties></domain></domains></qubes>',
                encoding="utf-8",
            )

            with patch("kuhbs.validation.QUBES_XML", qubes_xml):
                with self.assertRaises(ConfigValidationError) as cm:
                    validate_startup_config(defaults, check_qubes=True)

            validation_source = (ROOT / "kuhbs/validation.py").read_text(encoding="utf-8")
            self.assertNotIn("CommandRunner(", validation_source)
            self.assertNotIn("vm_exists(", validation_source)
            self.assertIn("template VM does not exist: debian-13-minimal", str(cm.exception))

    def test_check_qubes_rejects_template_name_with_wrong_qube_class(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            write_definition(root, valid_definition("good"))
            qubes_xml = root / "qubes.xml"
            qubes_xml.write_text(
                '<qubes><domains><domain class="AppVM"><properties>'
                '<property name="name">debian-13-minimal</property>'
                '</properties></domain></domains></qubes>',
                encoding="utf-8",
            )

            with patch("kuhbs.validation.QUBES_XML", qubes_xml):
                with self.assertRaises(ConfigValidationError) as cm:
                    validate_startup_config(defaults, check_qubes=True)

            self.assertIn("template VM must be a TemplateVM: debian-13-minimal is AppVM", str(cm.exception))

    def test_check_qubes_returns_the_parsed_xml_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            write_definition(root, valid_definition("good"))
            qubes_xml = root / "qubes.xml"
            qubes_xml.write_text(
                '<qubes><domains><domain class="TemplateVM"><properties>'
                '<property name="name">debian-13-minimal</property>'
                '</properties></domain></domains></qubes>',
                encoding="utf-8",
            )

            with patch("kuhbs.validation.QUBES_XML", qubes_xml):
                snapshot = inspect_startup_config(defaults, check_qubes=True)

            self.assertEqual(snapshot.qubes_vms["debian-13-minimal"].klass, "TemplateVM")

    def test_check_qubes_rejects_unreadable_or_malformed_qubes_xml_globally(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            for name, path in (
                ("missing", root / "missing.xml"),
                ("malformed", root / "malformed.xml"),
                ("no-domains", root / "no-domains.xml"),
            ):
                if name == "malformed":
                    path.write_text("<qubes>", encoding="utf-8")
                elif name == "no-domains":
                    path.write_text("<qubes/>", encoding="utf-8")
                with self.subTest(name=name):
                    with patch("kuhbs.validation.QUBES_XML", path):
                        with self.assertRaises(ConfigValidationError) as cm:
                            validate_startup_config(defaults, check_qubes=True)
                    self.assertIn(str(path), str(cm.exception))

    def test_tpl_only_kuhb_is_not_valid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            bad = root / "config/my-kuhbs/bad/kuhb.yml"
            bad.parent.mkdir(parents=True)
            (bad.parent / "icon.svg").write_text("<svg/>", encoding="utf-8")
            bad.write_text("""id: bad
name: Bad
description: Template-only definitions are not user-facing kuhbs
icon: icon.svg
order: 500
type: tpl
template: debian-13-minimal
kuhs:
  tpl: {}
""", encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("type must be one of app, sta, ndp, udp", str(cm.exception))

    def test_app_type_implies_tpl_kuh(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            good = root / "config/my-kuhbs/good/kuhb.yml"
            good.parent.mkdir(parents=True)
            (good.parent / "icon.svg").write_text("<svg/>", encoding="utf-8")
            good.write_text("""id: good
name: Good
description: Template kuh is implied by app type
icon: icon.svg
order: 500
type: app
template: debian-13-minimal
kuhs:
  app:
    instances:
      - id: default
        prefs:
          label: green
""", encoding="utf-8")

            validated = validate_startup_config(defaults)

            self.assertIn("tpl", validated.kuhb_definitions[0]["kuhs"])

    def test_ndp_type_requires_tpl_app_and_ndp_kuhs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            bad = root / "config/my-kuhbs/bad/kuhb.yml"
            bad.parent.mkdir(parents=True)
            (bad.parent / "icon.svg").write_text("<svg/>", encoding="utf-8")
            bad.write_text("""id: bad
name: Bad
description: Missing ndp dependency chain
icon: icon.svg
order: 500
type: ndp
template: debian-13-minimal
kuhs:
  tpl: {}
  ndp:
    instances:
      - id: default
        prefs:
          label: red
""", encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("kuhb type ndp requires kuhs: app", str(cm.exception))

    def test_app_kuhs_must_use_instance_list_with_ids(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            bad = root / "config/my-kuhbs/bad/kuhb.yml"
            bad.parent.mkdir(parents=True)
            (bad.parent / "icon.svg").write_text("<svg/>", encoding="utf-8")
            bad.write_text("""id: bad
name: Bad
description: App singleton shape is not valid
icon: icon.svg
order: 500
type: app
template: debian-13-minimal
kuhs:
  tpl: {}
  app: {}
""", encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("kuhs.app.instances must have at least one item", str(cm.exception))

    def test_raw_kuh_shapes_are_validated_before_defaults_merge(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            bad = root / "config/my-kuhbs/bad/kuhb.yml"
            bad.parent.mkdir(parents=True)
            (bad.parent / "icon.svg").write_text("<svg/>", encoding="utf-8")
            bad.write_text("""id: bad
name: Bad
description: Raw shape errors must not crash merge
icon: icon.svg
order: 500
type: app
template: debian-13-minimal
kuhs:
  app: nope
  foo: {}
""", encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            message = str(cm.exception)
            self.assertIn("kuhs.app must be a mapping", message)
            self.assertIn("kuhs.foo is not a supported key", message)

    def test_unknown_top_level_kuhb_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            bad = root / "config/my-kuhbs/bad/kuhb.yml"
            bad.parent.mkdir(parents=True)
            (bad.parent / "icon.svg").write_text("<svg/>", encoding="utf-8")
            bad.write_text("""id: bad
name: Bad
description: Runtime-only keys must not come from YAML
icon: icon.svg
order: 500
type: app
template: debian-13-minimal
kuhb_dir: /tmp/other
kuhs:
  app:
    instances:
      - id: default
        prefs:
          label: green
""", encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("kuhb_dir is not a supported key", str(cm.exception))

    def test_ndp_and_udp_app_instances_must_be_dispvm_templates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            bad = root / "config/my-kuhbs/bad/kuhb.yml"
            bad.parent.mkdir(parents=True)
            (bad.parent / "icon.svg").write_text("<svg/>", encoding="utf-8")
            bad.write_text("""id: bad
name: Bad
description: App must back disposables
icon: icon.svg
order: 500
type: udp
template: debian-13-minimal
kuhs:
  tpl: {}
  app:
    instances:
      - id: default
        prefs:
          label: green
""", encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("kuhb type udp requires kuhs.app.instances[0].prefs.template_for_dispvms: True", str(cm.exception))

    def test_optional_ndp_requires_default_app_dispvm_template(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            bad = root / "config/my-kuhbs/bad/kuhb.yml"
            bad.parent.mkdir(parents=True)
            (bad.parent / "icon.svg").write_text("<svg/>", encoding="utf-8")
            bad.write_text("""id: bad
name: Bad
description: Optional ndp needs a disposable backing app
icon: icon.svg
order: 500
type: app
template: debian-13-minimal
kuhs:
  app:
    instances:
      - id: default
        prefs:
          label: green
          template_for_dispvms: False
  ndp:
    instances:
      - id: default
""", encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("configured kuhs.ndp requires default app prefs.template_for_dispvms: True", str(cm.exception))

    def test_volume_ephemeral_must_be_boolean(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            bad = root / "config/my-kuhbs/bad/kuhb.yml"
            bad.parent.mkdir(parents=True)
            (bad.parent / "icon.svg").write_text("<svg/>", encoding="utf-8")
            bad.write_text("""id: bad
name: Bad
description: Quoted boolean should fail
icon: icon.svg
order: 500
type: app
template: debian-13-minimal
kuhs:
  tpl:
    volume_ephemeral: "False"
  app:
    instances:
      - id: default
        prefs:
          label: green
""", encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("kuhs.tpl.volume_ephemeral must be True or False", str(cm.exception))

    def test_missing_icon_file_is_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            bad = root / "config/my-kuhbs/bad/kuhb.yml"
            bad.parent.mkdir(parents=True)
            bad.write_text("""id: bad
name: Bad
description: Missing icon file
icon: missing.svg
order: 500
type: sta
template: debian-13-minimal
kuhs:
  sta:
    instances:
      - id: default
        prefs:
          label: orange
""", encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("icon file does not exist: missing.svg", str(cm.exception))

    def test_icon_file_must_be_valid_svg(self):
        cases = {
            "malformed XML": "<svg>",
            "wrong root element": "<html/>",
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults_path = write_defaults(root)
            defaults = load_defaults(defaults_path)
            for label, content in cases.items():
                with self.subTest(label=label):
                    data = valid_definition()
                    path = write_definition(root, data)
                    (path.parent / data["icon"]).write_text(content, encoding="utf-8")

                    with self.assertRaises(ConfigValidationError) as cm:
                        validate_kuhb_file(defaults, path)

                    self.assertIn("icon file is not a valid SVG", str(cm.exception))

    def test_every_launcher_requires_a_valid_svg(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults_path = write_defaults(root)
            defaults = load_defaults(defaults_path)
            data = valid_definition()
            data["kuhs"]["app"]["instances"][0]["launchers"] = [{
                "id": "signal",
                "name": "Signal",
                "user": "user",
                "command": "/usr/bin/signal-desktop",
                "dispvm": False,
                "run_in_terminal": False,
                "shutdown_on_exit_0": True,
            }]
            path = write_definition(root, data)
            launcher_icon = path.parent / "launcher-icons/signal.svg"

            launcher_icon.unlink()
            with self.assertRaises(ConfigValidationError) as cm:
                validate_kuhb_file(defaults, path)
            self.assertIn("launcher icon file does not exist: launcher-icons/signal.svg", str(cm.exception))

            launcher_icon.write_text("<svg>", encoding="utf-8")
            with self.assertRaises(ConfigValidationError) as cm:
                validate_kuhb_file(defaults, path)
            self.assertIn("launcher icon signal.svg is not a valid SVG", str(cm.exception))

    def test_unreferenced_launcher_svg_must_also_be_valid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults_path = write_defaults(root)
            defaults = load_defaults(defaults_path)
            path = write_definition(root, valid_definition())
            launcher_dir = path.parent / "launcher-icons"
            launcher_dir.mkdir()
            (launcher_dir / "unused.svg").write_text("<html/>", encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_kuhb_file(defaults, path)

            self.assertIn("launcher icon unused.svg is not a valid SVG", str(cm.exception))

    def test_backup_paths_shape_is_startup_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            bad = root / "config/my-kuhbs/bad/kuhb.yml"
            bad.parent.mkdir(parents=True)
            (bad.parent / "icon.svg").write_text("<svg/>", encoding="utf-8")
            bad.write_text("""id: bad
name: Bad
description: Empty backup paths
icon: icon.svg
order: 500
type: sta
template: debian-13-minimal
kuhs:
  sta:
    instances:
      - id: default
        prefs:
          label: green
        backup:
          paths: []
""", encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("kuhs.sta.instances[0].backup.paths must have at least one item", str(cm.exception))

    def test_setup_script_paths_do_not_require_existing_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults_path = write_defaults(root)
            defaults = load_defaults(defaults_path)
            data = valid_definition()
            data["kuhs"]["app"]["instances"][0]["setup_scripts"] = ["/not/installed/yet.sh"]
            path = write_definition(root, data)

            validated = validate_kuhb_file(defaults, path)

            self.assertEqual(validated["id"], "bad")

            data["kuhs"]["app"]["instances"][0]["setup_scripts"] = ["relative.sh"]
            path = write_definition(root, data)

            with self.assertRaises(ConfigValidationError) as cm:
                validate_kuhb_file(defaults, path)

            self.assertIn("setup_scripts[0] must be an absolute path", str(cm.exception))

    def test_defaults_runtime_types_are_strict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = yaml.safe_load(defaults.read_text(encoding="utf-8"))
            data["backup"]["max_age_hours"] = True
            data["upgrade"]["max_age_minutes"] = True
            data["logging"]["spacing"] = True
            defaults.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            message = str(cm.exception)
            self.assertIn("backup.max_age_hours must be a positive number", message)
            self.assertIn("upgrade.max_age_minutes must be a positive number", message)
            self.assertIn("logging.spacing must be a positive integer", message)

    def test_firewall_defaults_are_required_kuhbs_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = yaml.safe_load(defaults.read_text(encoding="utf-8"))
            data.pop("firewall")
            defaults.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("firewall", str(cm.exception))

    def test_launcher_text_must_match_schema_format(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            bad = root / "config/my-kuhbs/bad/kuhb.yml"
            bad.parent.mkdir(parents=True)
            (bad.parent / "icon.svg").write_text("<svg/>", encoding="utf-8")
            bad.write_text("""id: bad
name: Bad
description: Launcher injection
icon: icon.svg
order: 500
type: app
template: debian-13-minimal
kuhs:
  tpl: {}
  app:
    instances:
      - id: default
        prefs:
          label: green
        launchers:
          - id: bad
            name: "Good\\nExec=/bin/evil"
            user: user
            command: /bin/true
            dispvm: False
            run_in_terminal: False
            shutdown_on_exit_0: False
""", encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("kuhs.app.instances[0].launchers[0].name has invalid format", str(cm.exception))

    def test_invalid_metadata_mappings_return_config_errors_instead_of_tracebacks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            for key in ("prefs", "services", "features"):
                with self.subTest(key=key):
                    data = valid_definition()
                    data["kuhs"]["app"]["instances"][0][key] = []
                    write_definition(root, data)

                    with self.assertRaises(ConfigValidationError) as cm:
                        validate_startup_config(defaults)

                    self.assertIn(f"{key} must be a mapping", str(cm.exception))

    def test_qvm_metadata_accepts_json_scalar_values_without_interpreting_them(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = valid_definition("scalars")
            instance = data["kuhs"]["app"]["instances"][0]
            instance["prefs"]["custom-ratio"] = 1.5
            instance["features"] = {
                "string": "value",
                "number": 2.5,
                "boolean": True,
                "null": None,
            }
            write_definition(root, data)

            validated = validate_startup_config(defaults)

            resolved = validated.kuhb_definitions[0]["kuhs"]["app"]["instances"][0]
            self.assertEqual(resolved["prefs"]["custom-ratio"], 1.5)
            self.assertEqual(
                resolved["features"],
                {"string": "value", "number": 2.5, "boolean": True, "null": None},
            )

    def test_qvm_metadata_rejects_nul_that_cannot_be_serialized_to_argv(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = valid_definition("nul-scalar")
            data["kuhs"]["app"]["instances"][0]["features"] = {"custom": "before\0after"}
            write_definition(root, data)

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("kuhs.app.instances[0].features.custom has invalid format", str(cm.exception))

    def test_standalone_with_optional_ndp_reports_missing_app_dependency(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = valid_definition()
            data["type"] = "sta"
            data["kuhs"] = {
                "sta": {"instances": [{"id": "default"}]},
                "ndp": {"instances": [{"id": "default"}]},
            }
            write_definition(root, data)

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("configured kuhs.ndp requires kuhs.app", str(cm.exception))

    def test_confirm_network_after_upgrade_must_be_boolean(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = valid_definition()
            data["confirm_network_after_upgrade"] = "True"
            write_definition(root, data)

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("confirm_network_after_upgrade must be True or False", str(cm.exception))

    def test_standalone_setup_template_derived_name_must_fit_qubes_limit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = valid_definition("a" * 26)
            data["type"] = "sta"
            data["kuhs"] = {"sta": {"instances": [{"id": "default"}]}}
            write_definition(root, data)

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("setup template resolves to invalid Qubes VM name", str(cm.exception))

    def test_optional_app_on_standalone_requires_template_kuh(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = valid_definition()
            data["type"] = "sta"
            data["kuhs"]["sta"] = {"instances": [{"id": "default"}]}
            write_definition(root, data)

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("configured kuhs.app requires kuhs.tpl", str(cm.exception))

    def test_optional_instances_must_define_ids_before_defaults_merge(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = valid_definition()
            data["type"] = "sta"
            data["kuhs"] = {
                "sta": {"instances": [{"id": "default"}]},
                "app": {"instances": [{"prefs": {"label": "green"}}]},
                "tpl": {},
            }
            write_definition(root, data)

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("kuhs.app.instances[0].id is required", str(cm.exception))

    def test_standalone_setup_template_rejects_ignored_keys(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = valid_definition()
            data["type"] = "sta"
            data["kuhs"] = {
                "sta": {
                    "setup_template": {"backup": {"paths": ["/home/user"]}, "launchers": []},
                    "instances": [{"id": "default"}],
                },
            }
            write_definition(root, data)

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            message = str(cm.exception)
            self.assertIn("kuhs.sta.setup_template.backup is not a supported key", message)
            self.assertIn("kuhs.sta.setup_template.launchers is not a supported key", message)

    def test_textual_none_is_not_a_valid_netvm_default(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = load_defaults(defaults)
            data["default_kuhb"]["kuhs"]["app"]["instances"][0]["prefs"]["netvm"] = "None"
            defaults.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("default_kuhb.kuhs.app.instances[0].prefs.netvm must use YAML null", str(cm.exception))

    def test_every_explicit_instance_kind_requires_at_least_one_instance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = valid_definition()
            data["type"] = "sta"
            data["kuhs"] = {
                "sta": {"instances": [{"id": "default"}]},
                "app": {"instances": []},
                "tpl": {},
            }
            write_definition(root, data)

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("kuhs.app.instances must have at least one item", str(cm.exception))

    def test_icon_path_must_not_contain_control_characters(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = valid_definition()
            data["icon"] = "icon.svg\nExec=/bin/evil"
            write_definition(root, data)

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("icon must not contain control characters", str(cm.exception))

    def test_defaults_reject_unsupported_terminal_emulators(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = load_defaults(defaults)
            data["terminal"]["path"] = "/usr/bin/gnome-terminal"
            defaults.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("terminal.path must be one of", str(cm.exception))

    def test_defaults_validate_complete_default_kuhb_shapes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = load_defaults(defaults)
            data["default_kuhb"]["kuhs"]["app"] = []
            defaults.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("default_kuhb.kuhs.app must be a mapping", str(cm.exception))

    def test_defaults_require_preferences_runtime_indexes_directly(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            cases = (
                ("tpl", "label"),
                ("app", "autostart"),
                ("app", "label"),
                ("app", "template_for_dispvms"),
                ("ndp", "autostart"),
                ("sta", "label"),
            )
            for kind, key in cases:
                with self.subTest(kind=kind, key=key):
                    data = load_defaults(ROOT / "defaults.yml")
                    data["paths"]["config"] = f"{root}/config"
                    data["paths"]["kuhbs"] = f"{root}/config/my-kuhbs"
                    if kind == "tpl":
                        prefs = data["default_kuhb"]["kuhs"][kind]["prefs"]
                    else:
                        prefs = data["default_kuhb"]["kuhs"][kind]["instances"][0]["prefs"]
                    prefs.pop(key)
                    defaults.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

                    with self.assertRaises(ConfigValidationError) as cm:
                        validate_startup_config(defaults)

                    self.assertIn(f"prefs.{key} is required", str(cm.exception))

    def test_qubes_labels_must_be_supported_names(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = valid_definition()
            data["kuhs"]["app"]["instances"][0]["prefs"]["label"] = "chartreuse"
            write_definition(root, data)

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("prefs.label must be one of", str(cm.exception))

    def test_udp_launcher_must_explicitly_set_shutdown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = valid_definition()
            data["type"] = "udp"
            instance = data["kuhs"]["app"]["instances"][0]
            instance["prefs"]["template_for_dispvms"] = True
            instance["launchers"] = [{
                "id": "browser",
                "name": "Browser",
                "user": "user",
                "command": "/usr/bin/browser",
                "dispvm": True,
                "run_in_terminal": False,
            }]
            write_definition(root, data)

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("shutdown_on_exit_0 is required", str(cm.exception))

    def test_non_udp_launcher_must_explicitly_set_shutdown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = valid_definition()
            data["kuhs"]["app"]["instances"][0]["launchers"] = [{
                "id": "browser",
                "name": "Browser",
                "user": "user",
                "command": "/usr/bin/browser",
                "dispvm": False,
                "run_in_terminal": False,
            }]
            write_definition(root, data)

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("kuhs.app.instances[0].launchers[0].shutdown_on_exit_0 is required", str(cm.exception))

    def test_udp_launcher_may_target_its_appvm(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = valid_definition()
            data["type"] = "udp"
            instance = data["kuhs"]["app"]["instances"][0]
            instance["prefs"]["template_for_dispvms"] = True
            instance["launchers"] = [{
                "id": "browser-config",
                "name": "Browser configuration",
                "user": "user",
                "command": "/usr/bin/browser-config",
                "dispvm": False,
                "run_in_terminal": False,
                "shutdown_on_exit_0": False,
            }]
            write_definition(root, data)

            validated = validate_startup_config(defaults)

            launcher = validated.kuhb_definitions[0]["kuhs"]["app"]["instances"][0]["launchers"][0]
            self.assertIs(launcher["dispvm"], False)

    def test_schema_formats_reject_trailing_control_characters(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults_path = write_defaults(root)
            defaults = load_defaults(defaults_path)
            cases = (
                ("icon", "icon.svg\n"),
                ("launcher id", "browser\n"),
                ("launcher name", "Browser\n"),
                ("launcher user", "user\n"),
                ("launcher command", "/usr/bin/browser\n"),
                ("instance id", "default\n"),
            )
            for label, value in cases:
                with self.subTest(label=label):
                    data = valid_definition()
                    if label == "icon":
                        data["icon"] = value
                    elif label == "instance id":
                        data["kuhs"]["app"]["instances"][0]["id"] = value
                    else:
                        launcher = {
                            "id": "browser",
                            "name": "Browser",
                            "user": "user",
                            "command": "/usr/bin/browser",
                            "dispvm": False,
                            "run_in_terminal": False,
                            "shutdown_on_exit_0": False,
                        }
                        launcher[label.removeprefix("launcher ")] = value
                        data["kuhs"]["app"]["instances"][0]["launchers"] = [launcher]
                    path = write_definition(root, data)

                    with self.assertRaises(ConfigValidationError) as cm:
                        validate_kuhb_file(defaults, path)

                    expected = "control characters" if label == "icon" else "invalid format"
                    self.assertIn(expected, str(cm.exception))

    def test_unhashable_yaml_keys_are_configuration_errors(self):
        malformed = "? [a, b]\n: c\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = root / "defaults.yml"
            defaults.write_text(malformed, encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("YAML parse error", str(cm.exception))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = valid_definition()
            definition = write_definition(root, data)
            definition.write_text(malformed, encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("YAML parse error", str(cm.exception))

    def test_qubes_vm_references_must_have_static_name_shape(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults_path = write_defaults(root)
            defaults = load_defaults(defaults_path)
            data = valid_definition()
            data["template"] = "--all"
            path = write_definition(root, data)

            with self.assertRaises(ConfigValidationError) as cm:
                validate_kuhb_file(defaults, path)

            self.assertIn("template has invalid format", str(cm.exception))

            data = valid_definition()
            data["kuhs"]["app"]["instances"][0]["prefs"]["netvm"] = "--all"
            path = write_definition(root, data)

            with self.assertRaises(ConfigValidationError) as cm:
                validate_kuhb_file(defaults, path)

            self.assertIn("prefs.netvm", str(cm.exception))

    def test_qubes_vm_references_must_begin_with_ascii_letter(self):
        definition_cases = (
            ("template", lambda data: data.__setitem__("template", "1-debian")),
            (
                "prefs.netvm",
                lambda data: data["kuhs"]["app"]["instances"][0]["prefs"].__setitem__("netvm", "1-netvm"),
            ),
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults_path = write_defaults(root)
            defaults = load_defaults(defaults_path)
            for expected, mutate in definition_cases:
                with self.subTest(expected=expected):
                    data = valid_definition()
                    mutate(data)
                    path = write_definition(root, data)

                    with self.assertRaises(ConfigValidationError) as cm:
                        validate_kuhb_file(defaults, path)

                    self.assertIn(expected, str(cm.exception))
                    self.assertIn("must begin with an ASCII letter", str(cm.exception))

    def test_cryptsetup_mapper_name_uses_one_safe_component(self):
        invalid_names = ("kuhbs/backups", ".", "..", "bad\nname", "x" * 128, "é" * 64)
        valid_names = ("kuhbs-backups", "backup mapper", "é" * 63)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            for value in invalid_names:
                with self.subTest(value=value):
                    data = load_defaults(defaults)
                    data["backup"]["crypt_name"] = value
                    defaults.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

                    with self.assertRaises(ConfigValidationError) as cm:
                        validate_startup_config(defaults)

                    self.assertIn("backup.crypt_name must be a valid cryptsetup mapper name", str(cm.exception))
            for value in valid_names:
                with self.subTest(value=value):
                    data = load_defaults(defaults)
                    data["backup"]["crypt_name"] = value
                    defaults.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

                    validate_startup_config(defaults)

    def test_candidate_rejects_duplicate_resolved_kuh_names(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults_path = write_defaults(root)
            defaults = load_defaults(defaults_path)
            data = valid_definition()
            data["kuhs"]["app"]["instances"].append({"id": "default", "prefs": {"label": "green"}})
            path = write_definition(root, data)

            with self.assertRaises(ConfigValidationError) as cm:
                validate_kuhb_file(defaults, path)

            self.assertIn("duplicate resolved KUH name app-bad", str(cm.exception))

    def test_definition_set_reserves_standalone_setup_vm_names(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            standalone = valid_definition("foo")
            standalone["type"] = "sta"
            standalone["kuhs"] = {"sta": {"instances": [{"id": "default"}]}}
            write_definition(root, standalone)
            write_definition(root, valid_definition("foo-setup-tmp"))

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults)

            self.assertIn("duplicate Qubes VM name tpl-foo-setup-tmp", str(cm.exception))

    def test_backup_paths_reject_explicit_dot_components(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults_path = write_defaults(root)
            defaults = load_defaults(defaults_path)
            data = valid_definition()
            data["kuhs"]["app"]["instances"][0]["backup"] = {"paths": ["/home/./user"]}
            path = write_definition(root, data)

            with self.assertRaises(ConfigValidationError) as cm:
                validate_kuhb_file(defaults, path)

            self.assertIn("must not contain ., .., or // path components", str(cm.exception))

    def test_workspace_attention_is_preserved_for_i3_integration(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults_path = write_defaults(root)
            defaults = load_defaults(defaults_path)
            data = valid_definition()
            instance = data["kuhs"]["app"]["instances"][0]
            instance["i3_integration_workspace_attention"] = "4|CODE"
            path = write_definition(root, data)

            merged = validate_kuhb_file(defaults, path)

            resolved = merged["kuhs"]["app"]["instances"][0]
            self.assertEqual(resolved["i3_integration_workspace_attention"], "4|CODE")

    def test_backup_paths_allow_shell_escaped_spaces(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults_path = write_defaults(root)
            defaults = load_defaults(defaults_path)
            data = valid_definition()
            path_value = r"/home/user/My\ Favorite\ Files"
            data["kuhs"]["app"]["instances"][0]["backup"] = {
                "paths": [path_value],
            }
            path = write_definition(root, data)

            merged = validate_kuhb_file(defaults, path)

            resolved = merged["kuhs"]["app"]["instances"][0]
            self.assertEqual(resolved["backup"]["paths"], [path_value])

    def test_backup_ignore_failed_read_is_preserved_per_kuh(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults_path = write_defaults(root)
            defaults = load_defaults(defaults_path)
            data = valid_definition()
            data["kuhs"]["app"]["instances"][0]["backup"] = {
                "paths": ["/home/user/.thunderbird"],
                "ignore_failed_read": True,
            }
            path = write_definition(root, data)

            merged = validate_kuhb_file(defaults, path)

            # The resolved concrete VM keeps its own live-file read policy
            resolved = merged["kuhs"]["app"]["instances"][0]
            self.assertIs(resolved["backup"]["ignore_failed_read"], True)

    def test_backup_paths_reject_shell_metacharacters(self):
        values = (
            "/home/user/$profile",
            "/home/user/`profile`",
            "/home/user/data;true",
            "/home/user/data|true",
            "/home/user/data>other",
            "/home/user/(data)",
            "/home/user/data&true",
            "/home/user/My Favorite Files",
            r"/home/user/bad\escape",
        )
        for value in values:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                defaults_path = write_defaults(root)
                defaults = load_defaults(defaults_path)
                data = valid_definition()
                data["kuhs"]["app"]["instances"][0]["backup"] = {"paths": [value]}
                path = write_definition(root, data)

                with self.assertRaises(ConfigValidationError) as cm:
                    validate_kuhb_file(defaults, path)

                self.assertIn("supports only absolute paths and * globs", str(cm.exception))

    def test_dom0_backup_paths_reject_shell_metacharacters(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults_path = write_defaults(root)
            data = load_defaults(defaults_path)
            data["backup"]["dom0_paths"] = ["/home/user/*;true"]
            defaults_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

            with self.assertRaises(ConfigValidationError) as cm:
                validate_startup_config(defaults_path)

            self.assertIn("supports only absolute paths and * globs", str(cm.exception))

    def test_unknown_installed_schema_dialect_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            schemas = root / "schemas"
            schemas.mkdir()
            (schemas / "defaults.schema.yml").write_text(
                "$schema: https://json-schema.org/draft/2020-12/schema\ntype: object\n",
                encoding="utf-8",
            )
            (schemas / "kuhb.schema.yml").write_text(
                """$schema: https://example.invalid/unknown-dialect
$id: kuhb.schema.yml
$defs:
  raw: {type: object}
  resolved: {type: object}
""",
                encoding="utf-8",
            )

            with patch("kuhbs.validation.SCHEMA_ROOT", schemas):
                with self.assertRaises(ConfigValidationError) as cm:
                    validate_startup_config(defaults)

            self.assertIn("invalid installed schema", str(cm.exception))

    def test_unknown_defaults_schema_dialect_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            schemas = root / "schemas"
            schemas.mkdir()
            (schemas / "defaults.schema.yml").write_text(
                "$schema: https://example.invalid/unknown-dialect\ntype: object\n",
                encoding="utf-8",
            )
            (schemas / "kuhb.schema.yml").write_text(
                """$schema: https://json-schema.org/draft/2020-12/schema
$id: kuhb.schema.yml
$defs:
  raw: {type: object}
  resolved: {type: object}
""",
                encoding="utf-8",
            )

            with patch("kuhbs.validation.SCHEMA_ROOT", schemas):
                with self.assertRaises(ConfigValidationError) as cm:
                    validate_startup_config(defaults)

            self.assertIn("invalid installed schema", str(cm.exception))


    def test_every_default_qubes_vm_reference_uses_static_name_shape(self):
        cases = (
            ("repos.dispvm_template", lambda data: data["repos"].__setitem__("dispvm_template", "1-dvm")),
            ("firewall.qubes_snitch.snitch_vm", lambda data: data["firewall"]["qubes_snitch"].__setitem__("snitch_vm", "1-snitch")),
            ("backup.kuh", lambda data: data["backup"].__setitem__("kuh", "1-backup")),
            (
                "upgrade.restart_without_prompt[0]",
                lambda data: data["upgrade"].__setitem__("restart_without_prompt", ["1-vm"]),
            ),
            (
                "default_kuhb.kuhs.tpl.prefs.netvm",
                lambda data: data["default_kuhb"]["kuhs"]["tpl"]["prefs"].__setitem__("netvm", "1-netvm"),
            ),
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            for expected, mutate in cases:
                with self.subTest(expected=expected):
                    data = load_defaults(ROOT / "defaults.yml")
                    data["paths"]["config"] = f"{root}/config"
                    data["paths"]["kuhbs"] = f"{root}/config/my-kuhbs"
                    mutate(data)
                    defaults.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

                    with self.assertRaises(ConfigValidationError) as cm:
                        validate_startup_config(defaults)

                    self.assertIn(expected, str(cm.exception))


    def test_product_schemas_and_bundled_definitions_validate(self):
        schema_root = ROOT / "schemas"
        for schema_name in ("defaults.schema.yml", "kuhb.schema.yml"):
            schema = yaml.safe_load((schema_root / schema_name).read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
        installer = (ROOT / "install/install.sh").read_text(encoding="utf-8")
        self.assertIn("python3-jsonschema", installer)
        self.assertIn("../schemas/*.yml /usr/share/kuhbs/schemas/", installer)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            data = load_defaults(defaults)
            bundled = ROOT / "install/templates/home/user/.kuhbs/my-kuhbs"
            data["paths"]["kuhbs"] = str(bundled)
            defaults.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

            validated = validate_startup_config(defaults)

            self.assertEqual(len(validated.kuhb_definitions), 4)

    def test_broken_installed_schema_reference_is_a_config_error(self):
        validator = Draft202012Validator({"$ref": "missing.schema.yml"})

        with self.assertRaises(ConfigValidationError) as cm:
            _schema_issues(Path("defaults.yml"), validator, {})

        self.assertIn("invalid installed schema reference", str(cm.exception))

    def test_invalid_linked_kuhb_is_structured_while_valid_sibling_remains_usable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            write_definition(root, valid_definition("good"))
            broken = root / "config/my-kuhbs/broken/kuhb.yml"
            broken.parent.mkdir(parents=True)
            (broken.parent / "icon.svg").write_text("<svg/>", encoding="utf-8")
            broken.write_text("name: Broken\nicon: icon.svg\n", encoding="utf-8")

            snapshot = inspect_startup_config(defaults)

            self.assertEqual([definition["id"] for definition in snapshot.kuhb_definitions], ["good"])
            self.assertEqual(len(snapshot.broken_kuhbs), 1)
            entry = snapshot.broken_kuhbs[0]
            self.assertEqual(entry.active_id, "broken")
            self.assertEqual(entry.path, broken)
            self.assertEqual(entry.definition.get("name"), "Broken")
            self.assertTrue(entry.issues)
            self.assertTrue(all(issue.path == broken for issue in entry.issues))
            self.assertIn("id is required", str(entry.error))

    def test_invalid_lifecycle_state_breaks_only_its_linked_kuhb(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            write_definition(root, valid_definition("good"))
            write_definition(root, valid_definition("broken"))
            state_file = root / "config/states/broken/create"
            state_file.parent.mkdir(parents=True)
            state_file.write_text("asdf\n", encoding="utf-8")

            snapshot = inspect_startup_config(defaults)

            self.assertEqual([definition["id"] for definition in snapshot.kuhb_definitions], ["good"])
            self.assertEqual([entry.active_id for entry in snapshot.broken_kuhbs], ["broken"])
            self.assertEqual(snapshot.broken_kuhbs[0].issues[0].path, state_file)
            self.assertIn("invalid lifecycle state", str(snapshot.broken_kuhbs[0].error))

    def test_cross_definition_collision_breaks_every_involved_definition(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            write_definition(root, valid_definition("good"))
            standalone = valid_definition("foo")
            standalone["type"] = "sta"
            standalone["kuhs"] = {"sta": {"instances": [{"id": "default"}]}}
            write_definition(root, standalone)
            write_definition(root, valid_definition("foo-setup-tmp"))

            snapshot = inspect_startup_config(defaults)

            self.assertEqual([definition["id"] for definition in snapshot.kuhb_definitions], ["good"])
            self.assertEqual({entry.active_id for entry in snapshot.broken_kuhbs}, {"foo", "foo-setup-tmp"})
            for entry in snapshot.broken_kuhbs:
                self.assertIn("duplicate Qubes VM name tpl-foo-setup-tmp", str(entry.error))

    def test_active_base_template_name_is_reserved_from_kuhb_ids(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            write_definition(root, valid_definition("signal"))
            write_definition(root, valid_definition("debian-13-minimal"))

            snapshot = inspect_startup_config(defaults)

            self.assertEqual([definition["id"] for definition in snapshot.kuhb_definitions], ["signal"])
            self.assertEqual([entry.active_id for entry in snapshot.broken_kuhbs], ["debian-13-minimal"])
            self.assertIn(
                "KUHB id debian-13-minimal is reserved by configured base TemplateVM debian-13-minimal",
                str(snapshot.broken_kuhbs[0].error),
            )

    def test_dom0_cannot_be_configured_as_a_base_template(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            definition = valid_definition("signal")
            definition["template"] = "dom0"
            write_definition(root, definition)

            snapshot = inspect_startup_config(defaults)

            self.assertEqual(snapshot.kuhb_definitions, ())
            self.assertEqual([entry.active_id for entry in snapshot.broken_kuhbs], ["signal"])
            self.assertIn("template must name a TemplateVM, not dom0", str(snapshot.broken_kuhbs[0].error))

    def test_unreadable_active_root_remains_a_hard_global_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            active_root = root / "config/my-kuhbs"

            with patch("kuhbs.validation.local_kuhb_paths", side_effect=OSError("permission denied")):
                with self.assertRaises(ConfigValidationError) as cm:
                    validate_startup_config(defaults)

            self.assertIn(str(active_root), str(cm.exception))
            self.assertIn("cannot read active KUHB directory: permission denied", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
