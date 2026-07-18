from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kuhbs.config import load_defaults, load_kuhb_definition
import kuhbs.launchers as launchers_module
from kuhbs.launchers import LauncherSpec, _desktop_path, generate_launchers, launcher_specs as raw_launcher_specs, remove_launchers
from helpers import MappingRunner, RecordingEventLogger as EventLogger, RunnerContext
from kuhbs.model import merge_kuhb_definition
from kuhbs.operations import create, remove


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "install/templates/home/user/.kuhbs/my-kuhbs"


def defaults(tmp_path):
    data = load_defaults(Path(__file__).resolve().parents[1] / "defaults.yml")
    data["paths"]["config"] = str(tmp_path / ".kuhbs")
    data["paths"]["kuhbs"] = str(FIXTURE_ROOT)
    data["paths"]["desktop_applications"] = str(tmp_path / ".local/share/applications")
    data["paths"]["setup_scripts"] = str(tmp_path / "usr/share/kuhbs/setup-scripts")
    data["paths"]["user_setup_scripts"] = str(tmp_path / ".kuhbs/setup-scripts")
    data["paths"]["scripts"] = str(tmp_path / ".kuhbs/work/scripts")
    return data


def context(tmp_path):
    runner = tmp_path / "usr/share/kuhbs/setup-scripts/kuhbs/kuhbs-run-script.sh"
    runner.parent.mkdir(parents=True)
    # Explicit fake execution still resolves the installed runner path used by production
    runner.write_text("#!/bin/bash\nexit 0\n")
    logger = EventLogger(log_path=tmp_path / "kuhbs.log", color=False, stdout=False)
    # Create fixtures model both managed VMs as absent before the preflight runs
    fake_runner = MappingRunner(
        logger,
        returncodes={
            ("qvm-check", "--quiet", "app-test-udp"): 1,
            ("qvm-check", "--quiet", "tpl-test-udp"): 1,
        },
    )
    return RunnerContext(defaults(tmp_path), logger, fake_runner)


def load_fixture(name: str) -> dict:
    return load_kuhb_definition(FIXTURE_ROOT / name / "kuhb.yml")


def definition():
    return load_fixture("test-udp")


def launcher_specs(config, data):
    # Runtime accepts startup-merged definitions, so tests merge raw fixtures explicitly
    return raw_launcher_specs(config, merge_kuhb_definition(config, data))


class LauncherTests(unittest.TestCase):
    def test_desktop_filename_preserves_component_boundaries(self):
        root = Path("/desktop")
        first = LauncherSpec("baz", "First", "app-foo", "bar-user", False, "true", False, False, "icon.svg")
        second = LauncherSpec("baz", "Second", "app-foo-bar", "user", False, "true", False, False, "icon.svg")

        self.assertEqual(_desktop_path(root, first).name, "kuhbs-app-foo+bar-user+baz.desktop")
        self.assertEqual(_desktop_path(root, second).name, "kuhbs-app-foo-bar+user+baz.desktop")

    def test_generate_launchers_writes_desktop_entries_only(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)

            generate_launchers(ctx, merge_kuhb_definition(ctx.defaults, definition()))

            names = sorted(path.name for path in (tmp_path / ".local/share/applications").iterdir())
            self.assertIn("kuhbs-app-test-udp+user+zen.desktop", names)
            self.assertTrue((tmp_path / ".local/share/applications/kuhbs-app-test-udp+user+zen.desktop").exists())

            desktop = (tmp_path / ".local/share/applications/kuhbs-app-test-udp+user+zen.desktop").read_text()
            self.assertIn("Name=KUHBS Zen (user@app-test-udp)", desktop)
            self.assertIn("Exec=/usr/bin/qvm-run --user user --dispvm app-test-udp", desktop)
            self.assertIn("/usr/bin/flatpak run app.zen_browser.zen", desktop)
            self.assertIn("/usr/bin/sudo --non-interactive /sbin/shutdown -h now", desktop)
            self.assertIn("Terminal=false", desktop)

    def test_launcher_specs_preserve_explicit_yaml_terminal_launcher(self):
        with tempfile.TemporaryDirectory() as td:
            specs = launcher_specs(defaults(Path(td)), definition())

        rows = [(spec.launcher_id, spec.target, spec.command) for spec in specs]
        self.assertIn(("zen", "app-test-udp", "/usr/bin/flatpak run app.zen_browser.zen"), rows)
        self.assertIn(("terminal", "app-test-udp", "terminal"), rows)
        self.assertNotIn(("terminal", "app-test-udp", "xfce4-terminal"), rows)
        self.assertNotIn(("terminal", "tpl-test-udp", "xfce4-terminal"), rows)


    def test_launcher_specs_do_not_add_implicit_terminal_launchers(self):
        data = load_fixture("test-app")
        for instance in data["kuhs"]["app"]["instances"]:
            instance["launchers"] = []

        with tempfile.TemporaryDirectory() as td:
            rows = [(spec.launcher_id, spec.target, spec.user, spec.dispvm, spec.command) for spec in launcher_specs(defaults(Path(td)), data)]

        self.assertEqual(rows, [])

    def test_launcher_specs_preserve_terminal_wrapped_commands(self):
        data = definition()
        data["kuhs"]["app"]["instances"][0]["launchers"] = [{
            "id": "hermes",
            "name": "Hermes",
            "user": "user",
            "dispvm": False,
            "command": "/usr/bin/xfce4-terminal --execute /home/user/.local/bin/hermes",
            "run_in_terminal": False,
            "shutdown_on_exit_0": False,
        }]

        with tempfile.TemporaryDirectory() as td:
            rows = [(spec.launcher_id, spec.target, spec.command) for spec in launcher_specs(defaults(Path(td)), data)]

        self.assertIn(("hermes", "app-test-udp", "/usr/bin/xfce4-terminal --execute /home/user/.local/bin/hermes"), rows)
        self.assertNotIn(("terminal", "app-test-udp", "/usr/bin/xfce4-terminal"), rows)

    def test_terminal_launcher_exec_preserves_dispvm_and_shutdown_options(self):
        data = definition()
        data["kuhs"]["app"]["instances"][0]["launchers"] = [{
            "id": "terminal",
            "name": "Terminal",
            "user": "user",
            "dispvm": True,
            "command": "terminal",
            "run_in_terminal": False,
            "shutdown_on_exit_0": True,
        }]

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            generate_launchers(ctx, merge_kuhb_definition(ctx.defaults, data))

            desktop = (tmp_path / ".local/share/applications/kuhbs-app-test-udp+user+terminal.desktop").read_text()

        self.assertIn("Exec=/usr/bin/kuhbs terminal app-test-udp user --dispvm --shutdown-on-exit-0", desktop)
        self.assertNotIn("Exec=/usr/bin/qvm-run", desktop)

    def test_remove_launchers_removes_stale_desktop_entries_for_resolved_kuh_names(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ctx = context(root)
            stale = root / ".local/share/applications/kuhbs-app-test-udp+user+old.desktop"
            stale.parent.mkdir(parents=True)
            stale.write_text("stale", encoding="utf-8")

            remove_launchers(ctx, merge_kuhb_definition(ctx.defaults, definition()))

            self.assertFalse(stale.exists())

    def test_remove_launchers_does_not_match_a_longer_target_name(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ctx = context(root)
            desktop_dir = root / ".local/share/applications"
            desktop_dir.mkdir(parents=True)
            owned = desktop_dir / "kuhbs-app-test-udp+user+removed.desktop"
            other = desktop_dir / "kuhbs-app-test-udp-extra+user+keep.desktop"
            owned.write_text("owned", encoding="utf-8")
            other.write_text("other", encoding="utf-8")

            remove_launchers(ctx, merge_kuhb_definition(ctx.defaults, definition()))

            self.assertFalse(owned.exists())
            self.assertTrue(other.exists())

    def test_remove_launchers_tolerates_parallel_empty_directory_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ctx = context(root)
            stale = root / ".local/share/applications/kuhbs-app-test-udp+user+old.desktop"
            stale.parent.mkdir(parents=True)
            stale.write_text("stale", encoding="utf-8")
            real_rmtree = launchers_module.shutil.rmtree

            def parallel_worker_wins(path):
                # Model another same-order Remove deleting the shared empty directory first
                real_rmtree(path)
                raise FileNotFoundError(path)

            with patch("kuhbs.launchers.shutil.rmtree", side_effect=parallel_worker_wins):
                remove_launchers(ctx, merge_kuhb_definition(ctx.defaults, definition()))

            self.assertFalse(stale.parent.exists())

    def test_explicit_tpl_launcher_is_allowed(self):
        data = load_fixture("test-app")
        data["kuhs"]["tpl"]["launchers"] = [{
            "id": "root-terminal",
            "name": "Root Terminal",
            "user": "root",
            "command": "terminal",
            "dispvm": False,
            "run_in_terminal": False,
            "shutdown_on_exit_0": False,
        }]

        with tempfile.TemporaryDirectory() as td:
            rows = [(spec.launcher_id, spec.target, spec.user, spec.command) for spec in launcher_specs(defaults(Path(td)), data)]

        self.assertIn(("root-terminal", "tpl-test-app", "root", "terminal"), rows)

    def test_desktop_exec_matches_direct_qvm_run_shape(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)

            generate_launchers(ctx, merge_kuhb_definition(ctx.defaults, definition()))

            desktop = (tmp_path / ".local/share/applications/kuhbs-app-test-udp+user+zen.desktop").read_text()
            self.assertIn("Exec=/usr/bin/qvm-run --user user --dispvm app-test-udp", desktop)
            self.assertIn("/usr/bin/xfce4-terminal --title", desktop)
            self.assertIn('"/usr/bin/xfce4-terminal --title \'app-test-udp: Zen\' --command', desktop)
            self.assertIn("--command", desktop)
            self.assertIn("bash -lc", desktop)
            self.assertIn("set -x; /usr/bin/flatpak run app.zen_browser.zen", desktop)
            self.assertNotIn("/home/user/kuhbs/scripts/qvm-launch.sh", desktop)

    def test_desktop_exec_quotes_and_escapes_reserved_characters(self):
        data = definition()
        data["kuhs"]["app"]["instances"][0]["launchers"] = [
            {
                "id": "reserved",
                "name": "Reserved",
                "user": "user",
                "dispvm": False,
                "command": "foo;bar$HOME%PATH",
                "run_in_terminal": False,
                "shutdown_on_exit_0": False,
            },
            {
                "id": "field-code",
                "name": "Field code",
                "user": "user",
                "dispvm": False,
                "command": "foo%bar",
                "run_in_terminal": False,
                "shutdown_on_exit_0": False,
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)

            generate_launchers(ctx, merge_kuhb_definition(ctx.defaults, data))

            desktop = (tmp_path / ".local/share/applications/kuhbs-app-test-udp+user+reserved.desktop").read_text()
            self.assertIn('Exec=/usr/bin/qvm-run --user user app-test-udp "foo;bar\\\\$HOME%%PATH"', desktop)
            field_code = (tmp_path / ".local/share/applications/kuhbs-app-test-udp+user+field-code.desktop").read_text()
            self.assertIn('Exec=/usr/bin/qvm-run --user user app-test-udp "foo%%bar"', field_code)

    def test_desktop_entries_use_valid_categories_and_icon(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)

            generate_launchers(ctx, merge_kuhb_definition(ctx.defaults, definition()))

            desktop = (tmp_path / ".local/share/applications/kuhbs-app-test-udp+user+zen.desktop").read_text()
            self.assertIn("Categories=System;X-KUHBS;", desktop)
            self.assertIn("Icon=", desktop)
            self.assertIn("test-udp/launcher-icons/zen.svg", desktop)

    def test_create_and_remove_manage_desktop_entries_with_fake_runner(self):
        # Fake Qubes execution must not suppress the real dom0 launcher filesystem contract
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            data = definition()
            data["kuhs"]["tpl"]["setup_scripts"] = []
            data = merge_kuhb_definition(ctx.defaults, data)
            create.run(ctx, data)
            self.assertTrue((tmp_path / ".local/share/applications/kuhbs-app-test-udp+user+zen.desktop").exists())

            # Launcher cleanup does not need to opt into unrelated Snitch policy removal
            with patch("builtins.input", return_value="n"):
                remove.run(ctx, data)
            self.assertFalse((tmp_path / ".local/share/applications/kuhbs-app-test-udp+user+zen.desktop").exists())


if __name__ == "__main__":
    unittest.main()
