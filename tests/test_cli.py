# Purpose: Regression tests for KUHBS CLI argument dispatch
# Scope: Verifies help routing without touching real Qubes state
import argparse
from contextlib import redirect_stdout
from contextlib import redirect_stderr
from pathlib import Path
import io
import os
import shlex
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from kuhbs import cli
from tests.test_validation import valid_definition, write_defaults, write_definition
from kuhbs.validation import BrokenKuhb, ConfigIssue


class CliTests(unittest.TestCase):
    def setUp(self):
        # CLI tests use isolated markers so they can never touch a real KUHBS session
        self.state_tmp = tempfile.TemporaryDirectory()
        state_root = Path(self.state_tmp.name) / "states"
        self.cli_marker = state_root / "cli"
        self.gui_marker = state_root / "gui"
        self.cli_marker_patch = patch.object(cli, "CLI_MARKER", self.cli_marker, create=True)
        self.gui_marker_patch = patch.object(cli, "GUI_MARKER", self.gui_marker, create=True)
        self.cli_marker_patch.start()
        self.gui_marker_patch.start()

    def tearDown(self):
        self.gui_marker_patch.stop()
        self.cli_marker_patch.stop()
        self.state_tmp.cleanup()

    def run_cli(self, argv):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            status = cli.main(argv)
        return status, output.getvalue()

    def test_existing_cli_marker_aborts_before_validation(self):
        self.cli_marker.parent.mkdir(parents=True)
        self.cli_marker.touch()

        with patch("kuhbs.cli.inspect_startup_config") as validate:
            status, output = self.run_cli(["check"])

        self.assertEqual(status, 1)
        self.assertIn("Another KUHBS CLI is already running", output)
        validate.assert_not_called()

    def test_manual_cli_aborts_while_gui_is_running(self):
        self.gui_marker.parent.mkdir(parents=True)
        self.gui_marker.touch()

        with patch("kuhbs.cli.inspect_startup_config") as validate:
            status, output = self.run_cli(["check"])

        self.assertEqual(status, 1)
        self.assertIn("Close the KUHBS GUI before running the CLI manually", output)
        validate.assert_not_called()

    def test_gui_child_uses_and_removes_cli_marker(self):
        self.gui_marker.parent.mkdir(parents=True)
        self.gui_marker.touch()
        validated = SimpleNamespace(defaults={}, kuhb_definitions=())

        def validate(*_args, **_kwargs):
            self.assertTrue(self.cli_marker.exists())
            return validated

        with patch.dict(os.environ, {"KUHBS_FROM_GUI": "1"}):
            with patch("kuhbs.cli.inspect_startup_config", side_effect=validate):
                status, _output = self.run_cli(["help"])

        self.assertEqual(status, 0)
        self.assertFalse(self.cli_marker.exists())
        self.assertTrue(self.gui_marker.exists())

    def test_cli_removes_marker_after_validation_failure(self):
        issue = ConfigIssue(Path("/tmp/defaults.yml"), "invalid")

        def validate(*_args, **_kwargs):
            self.assertTrue(self.cli_marker.exists())
            raise cli.ConfigValidationError([issue])

        with patch("kuhbs.cli.inspect_startup_config", side_effect=validate):
            status, _output = self.run_cli(["check"])

        self.assertEqual(status, 1)
        self.assertFalse(self.cli_marker.exists())

    def run_completion_list(self, words: list[str], home: Path) -> list[str]:
        # Execute the installed Bash function so tests cover the actual COMP_WORDS contract
        completion = Path(__file__).resolve().parents[1] / "install/templates/etc/bash_completion.d/kuhbs"
        shell_words = " ".join(shlex.quote(word) for word in words)
        script = (
            f"source {shlex.quote(str(completion))}; "
            f"COMP_WORDS=({shell_words}); COMP_CWORD={len(words) - 1}; "
            "_kuhbs; printf '%s\\n' \"${COMPREPLY[@]}\""
        )
        result = subprocess.run(
            ["bash", "-c", script],
            check=True,
            text=True,
            capture_output=True,
            env={**os.environ, "HOME": str(home), "PATH": f"{home / 'bin'}:{os.environ['PATH']}"},
        )
        return [line for line in result.stdout.splitlines() if line]

    def run_completion(self, words: list[str], home: Path) -> set[str]:
        # Every candidate must remain unique even when several completion sources overlap
        completed = self.run_completion_list(words, home)
        self.assertEqual(len(completed), len(set(completed)), words)
        return set(completed)

    def test_explicit_create_aborts_when_any_requested_kuhb_cannot_run(self):
        class FakeState:
            def current_gate(self, kuhb_id):
                return {"alpha": "linked", "bravo": "create:completed"}[kuhb_id]

        definitions = ({"id": "alpha", "order": 10}, {"id": "bravo", "order": 20})
        snapshot = SimpleNamespace(defaults={}, kuhb_definitions=definitions, broken_kuhbs=())
        ctx = SimpleNamespace(logger=Mock(), state_store=FakeState())

        with patch("kuhbs.cli.inspect_startup_config", return_value=snapshot), \
             patch("kuhbs.cli.make_context", return_value=ctx), \
             patch("kuhbs.cli.create.run_all") as run_all, \
             patch("builtins.input") as prompt:
            status, output = self.run_cli(["create", "alpha", "bravo"])

        self.assertEqual(status, 1)
        self.assertIn("bravo: state is create:completed", output)
        self.assertNotIn("will run:", output)
        run_all.assert_not_called()
        prompt.assert_not_called()

    def test_bare_kuhbs_shows_help(self):
        validated = SimpleNamespace(defaults={}, kuhb_definitions=())
        with patch("kuhbs.cli.inspect_startup_config", return_value=validated):
            status, output = self.run_cli([])

        self.assertEqual(status, 0)
        self.assertIn("usage: kuhbs [global options] <command> [command options]", output)
        self.assertIn("create", output)
        self.assertIn("backup-all", output)
        self.assertIn("terminal", output)

    def test_terminal_user_is_optional_positional_arg(self):
        parser = cli.build_parser()

        default_args = parser.parse_args(["terminal", "app-signal"])
        root_args = parser.parse_args(["terminal", "app-signal", "root"])

        # KUHBS cannot know guest accounts from dom0, so it forwards any user string to qvm-run
        self.assertEqual(default_args.user, "user")
        self.assertEqual(root_args.user, "root")

    def test_terminal_accepts_launcher_behavior_options(self):
        parser = cli.build_parser()

        args = parser.parse_args(["terminal", "app-hermes", "user", "--dispvm", "--shutdown-on-exit-0"])

        self.assertTrue(args.dispvm)
        self.assertTrue(args.shutdown_on_exit_0)

    def test_upgrade_accepts_multiple_kuhbs(self):
        parser = cli.build_parser()

        args = parser.parse_args(["upgrade", "a", "b", "dom0"])

        self.assertEqual(args.kuhb, ["a", "b", "dom0"])

    def test_upgrade_all_is_a_command(self):
        parser = cli.build_parser()

        args = parser.parse_args(["upgrade-all"])

        self.assertEqual(args.command, "upgrade-all")

    def test_create_all_is_a_command(self):
        parser = cli.build_parser()

        args = parser.parse_args(["create-all"])

        self.assertEqual(args.command, "create-all")

    def test_backup_all_is_a_command(self):
        parser = cli.build_parser()

        args = parser.parse_args(["backup-all"])

        self.assertEqual(args.command, "backup-all")


    def test_backup_and_restore_accept_multiple_targets(self):
        parser = cli.build_parser()

        backup_args = parser.parse_args(["backup", "alpha", "app-bravo", "dom0"])
        restore_args = parser.parse_args(["restore", "alpha", "app-bravo", "dom0"])

        self.assertEqual(backup_args.target, ["alpha", "app-bravo", "dom0"])
        self.assertEqual(restore_args.target, ["alpha", "app-bravo", "dom0"])

    def test_restore_all_is_a_command(self):
        parser = cli.build_parser()

        args = parser.parse_args(["restore-all"])

        self.assertEqual(args.command, "restore-all")

    def test_backup_mount_commands_are_commands(self):
        parser = cli.build_parser()

        mount_args = parser.parse_args(["backup-mount"])
        umount_args = parser.parse_args(["backup-umount"])

        self.assertEqual(mount_args.command, "backup-mount")
        self.assertEqual(umount_args.command, "backup-umount")

    def test_repo_add_accepts_optional_branch_and_update_repo_requires_repo(self):
        parser = cli.build_parser()

        default_branch = parser.parse_args(["repo-add", "https://github.com/foo/bar"])
        custom_branch = parser.parse_args(["repo-add", "https://github.com/foo/bar", "master"])
        update = parser.parse_args(["update-repo", "github.com/foo/bar"])

        self.assertIsNone(default_branch.branch)
        self.assertEqual(custom_branch.branch, "master")
        self.assertEqual(update.repo, "github.com/foo/bar")

    def test_link_and_unlink_accept_multiple_sources(self):
        parser = cli.build_parser()

        link = parser.parse_args(["link", "github.com/foo/bar/alpha", "github.com/foo/bar/bravo"])
        unlink = parser.parse_args(["unlink", "github.com/foo/bar/alpha", "github.com/foo/bar/bravo"])

        self.assertEqual(link.source, ["github.com/foo/bar/alpha", "github.com/foo/bar/bravo"])
        self.assertEqual(unlink.source, ["github.com/foo/bar/alpha", "github.com/foo/bar/bravo"])

    def test_link_and_unlink_dispatch_one_batch(self):
        validated = SimpleNamespace(defaults={}, kuhb_definitions=())
        ctx = Mock()
        link_sources = ["github.com/foo/bar/alpha", "github.com/foo/bar/bravo"]
        unlink_sources = ["github.com/foo/bar/charlie", "github.com/foo/bar/delta"]

        with patch("kuhbs.cli.inspect_startup_config", return_value=validated):
            with patch("kuhbs.cli.make_context", return_value=ctx):
                with patch("kuhbs.cli.repository.link_kuhbs") as link_batch:
                    link_status, _output = self.run_cli(["link", *link_sources])
                with patch("kuhbs.cli.repository.unlink_kuhbs") as unlink_batch:
                    unlink_status, _output = self.run_cli(["unlink", *unlink_sources])

        self.assertEqual((link_status, unlink_status), (0, 0))
        link_batch.assert_called_once_with(ctx, link_sources)
        unlink_batch.assert_called_once_with(ctx, unlink_sources)

    def test_link_and_unlink_completion_offer_remaining_sources(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            repo = home / ".kuhbs/repos/github.com/foo/bar"
            (repo / ".git").mkdir(parents=True)
            for kuhb_id in ("alpha", "bravo"):
                kuhb = repo / kuhb_id
                kuhb.mkdir()
                (kuhb / "kuhb.yml").write_text(f"id: {kuhb_id}\n", encoding="utf-8")
            link = self.run_completion(
                ["kuhbs", "link", "github.com/foo/bar/alpha", ""],
                home,
            )

            active = home / ".kuhbs/my-kuhbs"
            active.mkdir(parents=True)
            for kuhb_id in ("alpha", "bravo"):
                (active / kuhb_id).symlink_to(repo / kuhb_id)
            unlink = self.run_completion(
                ["kuhbs", "unlink", "github.com/foo/bar/alpha", ""],
                home,
            )

        self.assertEqual(link, {"github.com/foo/bar/bravo"})
        self.assertEqual(unlink, {"github.com/foo/bar/bravo"})

    def test_repo_completion_lists_only_real_top_level_checkouts(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            repos = home / ".kuhbs/repos"
            valid = repos / "github.com/foo/bar"
            (valid / ".git").mkdir(parents=True)
            (valid / "nested/.git").mkdir(parents=True)
            (repos / "github.com/foo/.hidden.new/.git").mkdir(parents=True)
            outside = home / "outside"
            (outside / ".git").mkdir(parents=True)
            (repos / "github.com/foo/link").symlink_to(outside, target_is_directory=True)
            completion = Path(__file__).resolve().parents[1] / "install/templates/etc/bash_completion.d/kuhbs"

            result = subprocess.run(
                ["bash", "-c", f"source {shlex.quote(str(completion))}; _kuhbs_complete_repos"],
                check=True,
                text=True,
                capture_output=True,
                env={**os.environ, "HOME": str(home)},
            )

            self.assertEqual(result.stdout.splitlines(), ["github.com/foo/bar"])

            (valid / "browser").mkdir()
            (valid / "browser/kuhb.yml").write_text("id: browser\n", encoding="utf-8")
            external_kuhb = home / "external-kuhb"
            external_kuhb.mkdir()
            (external_kuhb / "kuhb.yml").write_text("id: external\n", encoding="utf-8")
            (valid / "linked").symlink_to(external_kuhb, target_is_directory=True)
            (valid / "bad-yaml").mkdir()
            (valid / "bad-yaml/kuhb.yml").symlink_to(external_kuhb / "kuhb.yml")

            link_result = subprocess.run(
                ["bash", "-c", f"source {shlex.quote(str(completion))}; _kuhbs_complete_repo_kuhbs"],
                check=True,
                text=True,
                capture_output=True,
                env={**os.environ, "HOME": str(home)},
            )

            self.assertEqual(link_result.stdout.splitlines(), ["github.com/foo/bar/browser"])

    def test_help_includes_backup_mount_commands(self):
        validated = SimpleNamespace(defaults={}, kuhb_definitions=())
        with patch("kuhbs.cli.inspect_startup_config", return_value=validated):
            status, output = self.run_cli(["help"])

        self.assertEqual(status, 0)
        self.assertIn("backup-mount", output)
        self.assertIn("backup-umount", output)

    def test_help_command_shows_help(self):
        validated = SimpleNamespace(defaults={}, kuhb_definitions=())
        with patch("kuhbs.cli.inspect_startup_config", return_value=validated):
            status, output = self.run_cli(["help"])

        self.assertEqual(status, 0)
        self.assertIn("usage: kuhbs [global options] <command> [command options]", output)

    def test_help_command_accepts_global_options(self):
        validated = SimpleNamespace(defaults={}, kuhb_definitions=())
        with patch("kuhbs.cli.inspect_startup_config", return_value=validated):
            status, output = self.run_cli(["--defaults", "defaults.yml", "help"])

        self.assertEqual(status, 0)
        self.assertIn("usage: kuhbs [global options] <command> [command options]", output)

    def test_removed_dry_run_option_is_rejected(self):
        # Execution avoidance is test-only now, so the public parser must not preserve a hidden compatibility path
        status, output = self.run_cli(["--dry-run", "help"])

        self.assertEqual(status, 2)
        self.assertIn("unrecognized arguments: --dry-run", output)

    def test_help_remains_available_with_broken_active_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            write_definition(root, valid_definition("browse"))
            state_file = root / "config/states/browse/create"
            state_file.parent.mkdir(parents=True)
            state_file.write_text("asdf\n", encoding="utf-8")

            status, output = self.run_cli(["--defaults", str(defaults), "help"])

        self.assertEqual(status, 0)
        self.assertIn("usage: kuhbs", output)
        self.assertNotIn("Configuration invalid", output)

    def test_gui_command_delegates_validation_to_gui(self):
        with patch("kuhbs.cli.inspect_startup_config") as inspect, \
             patch("kuhbs.gui.main", return_value=0) as run_gui:
            status, _output = self.run_cli(["--defaults", "/tmp/defaults.yml", "gui"])

        self.assertEqual(status, 0)
        inspect.assert_not_called()
        run_gui.assert_called_once_with(["--defaults", "/tmp/defaults.yml"])

    def test_unknown_command_shows_help_and_fails(self):
        status, output = self.run_cli(["nonsense"])

        self.assertEqual(status, 2)
        self.assertIn("invalid choice: 'nonsense'", output)

    def test_unknown_command_after_global_options_shows_help(self):
        status, output = self.run_cli(["--defaults", "defaults.yml", "wat"])

        self.assertEqual(status, 2)
        self.assertIn("invalid choice: 'wat'", output)

    def test_bash_completion_command_list_matches_argparse(self):
        # The small shell list stays handwritten, so this test catches command drift
        parser = cli.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        parser_commands = set(subparsers.choices)
        completion = Path(__file__).resolve().parents[1] / "install/templates/etc/bash_completion.d/kuhbs"
        result = subprocess.run(
            ["bash", "-c", f"source {shlex.quote(str(completion))}; _kuhbs_complete_commands"],
            check=True,
            text=True,
            capture_output=True,
        )

        self.assertEqual(set(result.stdout.splitlines()), parser_commands)

    def test_multi_target_completion_uses_only_supported_kuhb_targets(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            kuhbs_dir = home / ".kuhbs/my-kuhbs"
            # Reserved dom0 debris must not overlap the explicit dom0 archive target
            for kuhb_id in ("alpha", "bravo", "dom0"):
                definition = kuhbs_dir / kuhb_id / "kuhb.yml"
                definition.parent.mkdir(parents=True)
                template = "debian-13-minimal" if kuhb_id != "bravo" else "whonix-workstation-18"
                definition.write_text(f"id: {kuhb_id}\ntemplate: {template}\n", encoding="utf-8")
            # A fake raw Qubes VM proves Backup and Restore do not leak arbitrary VM names into completion
            fake_qvm_ls = home / "bin/qvm-ls"
            fake_qvm_ls.parent.mkdir()
            fake_qvm_ls.write_text("#!/bin/sh\nprintf 'NAME\\nwork-vm\\n'\n", encoding="utf-8")
            fake_qvm_ls.chmod(0o755)

            local_targets = {"alpha", "bravo"}
            cases = (
                ("create", local_targets),
                ("remove", local_targets),
                ("upgrade", local_targets | {"dom0", "debian-13-minimal", "whonix-workstation-18"}),
                ("backup", local_targets | {"dom0"}),
                ("restore", local_targets | {"dom0"}),
            )
            for command, expected in cases:
                with self.subTest(command=command):
                    completed = self.run_completion(["kuhbs", command, "alpha", ""], home)
                    self.assertEqual(completed, expected)

    def test_global_option_completion_works_after_the_command(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)

            for words in (["kuhbs", "check", "--d"], ["kuhbs", "create", "alpha", "--d"]):
                with self.subTest(words=words):
                    self.assertEqual(self.run_completion(words, home), {"--defaults"})

    def test_defaults_completion_preserves_whitespace_before_and_after_command(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            defaults = home / "defaults file.yml"
            defaults.write_text("{}\n", encoding="utf-8")
            prefix = str(home / "defaults f")

            separate_cases = (["kuhbs", "--defaults", prefix], ["kuhbs", "check", "--defaults", prefix])
            for words in separate_cases:
                with self.subTest(form="separate", words=words):
                    self.assertEqual(self.run_completion(words, home), {str(defaults)})

            current = f"--defaults={prefix}"
            for words in (["kuhbs", current], ["kuhbs", "check", current]):
                with self.subTest(form="equals", words=words):
                    self.assertEqual(self.run_completion(words, home), {f"--defaults={defaults}"})

    def test_defaults_equals_handles_bash_wordbreak_layout(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            defaults = home / "defaults.yml"
            defaults.write_text("{}\n", encoding="utf-8")
            prefix = str(home / "def")
            fake_qvm_ls = home / "bin/qvm-ls"
            fake_qvm_ls.parent.mkdir()
            fake_qvm_ls.write_text("#!/bin/sh\nprintf 'NAME\\nwork-vm\\n'\n", encoding="utf-8")
            fake_qvm_ls.chmod(0o755)

            for words in (
                ["kuhbs", "--defaults", "=", prefix],
                ["kuhbs", "terminal", "--defaults", "=", prefix],
            ):
                with self.subTest(stage="file", words=words):
                    self.assertEqual(self.run_completion(words, home), {str(defaults)})

            for words in (
                ["kuhbs", "--defaults", "=", str(defaults), "terminal", ""],
                ["kuhbs", "terminal", "--defaults", "=", str(defaults), ""],
            ):
                with self.subTest(stage="target", words=words):
                    self.assertEqual(self.run_completion(words, home), {"dom0", "work-vm"})

    def test_defaults_completion_enables_readline_filename_quoting(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            completion = Path(__file__).resolve().parents[1] / "install/templates/etc/bash_completion.d/kuhbs"
            prefix = str(home / "defaults f")
            script = (
                f"source {shlex.quote(str(completion))}; "
                "compopt() { printf '%s\\n' \"$@\"; }; "
                f"COMP_WORDS=(kuhbs --defaults {shlex.quote(prefix)}); COMP_CWORD=2; _kuhbs"
            )

            result = subprocess.run(["bash", "-c", script], check=True, text=True, capture_output=True)

            self.assertEqual(result.stdout.splitlines(), ["-o", "filenames"])

    def test_terminal_completion_lists_dom0_once(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            fake_qvm_ls = home / "bin/qvm-ls"
            fake_qvm_ls.parent.mkdir()
            fake_qvm_ls.write_text("#!/bin/sh\nprintf 'NAME\\ndom0\\nwork-vm\\n'\n", encoding="utf-8")
            fake_qvm_ls.chmod(0o755)

            completed = self.run_completion_list(["kuhbs", "terminal", ""], home)

            self.assertEqual(completed.count("dom0"), 1)
            self.assertEqual(set(completed), {"dom0", "work-vm"})

    def test_end_of_options_marker_does_not_consume_a_positional_slot(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            repo = home / ".kuhbs/repos/github.com/foo/bar"
            (repo / ".git").mkdir(parents=True)
            fake_qvm_ls = home / "bin/qvm-ls"
            fake_qvm_ls.parent.mkdir()
            fake_qvm_ls.write_text("#!/bin/sh\nprintf 'NAME\\nwork-vm\\n'\n", encoding="utf-8")
            fake_qvm_ls.chmod(0o755)

            self.assertEqual(
                self.run_completion(["kuhbs", "terminal", "--", ""], home),
                {"dom0", "work-vm"},
            )
            self.assertEqual(
                self.run_completion(["kuhbs", "update-repo", "--", ""], home),
                {"github.com/foo/bar"},
            )
            # After --, a Terminal-looking flag is the target rather than an option
            self.assertEqual(
                self.run_completion(["kuhbs", "terminal", "--", "--dispvm", ""], home),
                {"user", "root"},
            )

    def test_terminal_completion_ignores_options_when_counting_positionals(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            fake_qvm_ls = home / "bin/qvm-ls"
            fake_qvm_ls.parent.mkdir()
            fake_qvm_ls.write_text("#!/bin/sh\nprintf 'NAME\\nwork-vm\\n'\n", encoding="utf-8")
            fake_qvm_ls.chmod(0o755)

            target_cases = (
                ["kuhbs", "terminal", "--dispvm", ""],
                ["kuhbs", "terminal", "--defaults", "/tmp/defaults.yml", ""],
            )
            for words in target_cases:
                with self.subTest(words=words):
                    self.assertEqual(self.run_completion(words, home), {"dom0", "work-vm"})

            self.assertEqual(
                self.run_completion(["kuhbs", "terminal", "work-vm", "--dispvm", ""], home),
                {"user", "root"},
            )

    def test_single_target_completion_resumes_after_global_options(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            repo = home / ".kuhbs/repos/github.com/foo/bar"
            (repo / ".git").mkdir(parents=True)
            repo_kuhb = repo / "browser"
            repo_kuhb.mkdir()
            (repo_kuhb / "kuhb.yml").write_text("id: browser\n", encoding="utf-8")
            linked = home / ".kuhbs/my-kuhbs/browser"
            linked.parent.mkdir(parents=True)
            linked.symlink_to(repo_kuhb, target_is_directory=True)

            cases = (
                ("update-repo", {"github.com/foo/bar"}),
                ("repo-remove", {"github.com/foo/bar"}),
                ("link", {"github.com/foo/bar/browser"}),
                ("unlink", {"github.com/foo/bar/browser"}),
            )
            option_forms = (("--defaults", "/tmp/defaults.yml"), ("--defaults=/tmp/defaults.yml",))
            for command, expected in cases:
                for options in option_forms:
                    with self.subTest(command=command, options=options):
                        words = ["kuhbs", command, *options, ""]
                        self.assertEqual(self.run_completion(words, home), expected)

    def test_terminal_completion_includes_launcher_options(self):
        completion = Path(__file__).resolve().parents[1] / "install/templates/etc/bash_completion.d/kuhbs"
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"source {shlex.quote(str(completion))}; COMP_WORDS=(kuhbs terminal app-hermes user --); COMP_CWORD=4; _kuhbs; printf '%s\\n' \"${{COMPREPLY[@]}}\"",
            ],
            check=True,
            text=True,
            capture_output=True,
        )

        self.assertEqual(set(result.stdout.splitlines()), {"--defaults", "--dispvm", "--shutdown-on-exit-0"})

    def test_global_options_work_after_command_too(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            qubes_xml = root / "qubes.xml"
            qubes_xml.write_text("<qubes><domains/></qubes>", encoding="utf-8")

            with patch("kuhbs.validation.QUBES_XML", qubes_xml):
                status, output = self.run_cli(["check", "--defaults", str(defaults)])

            self.assertEqual(status, 0)
            self.assertEqual(output, "Configuration validated\n")

    def test_check_validates_config_with_qubes_xml_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)
            qubes_xml = root / "qubes.xml"
            qubes_xml.write_text("<qubes><domains/></qubes>", encoding="utf-8")

            with patch("kuhbs.validation.QUBES_XML", qubes_xml):
                status, output = self.run_cli(["--defaults", str(defaults), "check"])

            self.assertEqual(status, 0)
            self.assertEqual(output, "Configuration validated\n")

    def test_backup_dom0_does_not_load_kuhb_definition(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            defaults = write_defaults(root)

            with patch("kuhbs.cli._execute_archive_plan", return_value=0):
                status, _output = self.run_cli(["--defaults", str(defaults), "backup", "dom0"])

            self.assertEqual(status, 0)
            self.assertFalse((root / "config/config/backup-dom0.yml").exists())

    def test_empty_definition_all_commands_fail_with_a_plan_message(self):
        # Empty Create-All and Remove-All must not look like successful automation
        snapshot = SimpleNamespace(defaults={}, kuhb_definitions=(), broken_kuhbs=())
        ctx = SimpleNamespace(logger=Mock(), state_store=Mock())

        commands = (
            ("create-all", "kuhbs.cli.create.run_all"),
            ("remove-all", "kuhbs.cli.remove.run_all"),
        )
        for command, target in commands:
            with self.subTest(command=command), \
                 patch("kuhbs.cli.inspect_startup_config", return_value=snapshot), \
                 patch("kuhbs.cli.make_context", return_value=ctx), \
                 patch(target) as run_all:
                status, output = self.run_cli([command])

            self.assertEqual(status, 1)
            self.assertIn("No eligible targets", output)
            run_all.assert_not_called()

    def test_create_all_prints_plan_and_dispatches_runnable_definitions(self):
        class FakeState:
            def current_gate(self, kuhb_id):
                return {"alpha": "linked", "system": "create:failed"}[kuhb_id]

        ctx = SimpleNamespace(logger=Mock(), state_store=FakeState())
        definitions = ({"id": "alpha", "order": 200}, {"id": "system", "order": 20})

        with patch("kuhbs.cli.inspect_startup_config", return_value=SimpleNamespace(defaults={}, kuhb_definitions=definitions)):
            with patch("kuhbs.cli.make_context", return_value=ctx):
                with patch("kuhbs.cli.create.run_all") as run_all, \
                     patch("builtins.input") as prompt:
                    status, output = self.run_cli(["create-all"])

        self.assertEqual(status, 0)
        self.assertIn("cannot run:\n  system: state is create:failed", output)
        self.assertIn("will run:\n  order 200: alpha", output)
        run_all.assert_called_once_with(ctx, ({"id": "alpha", "order": 200},))
        prompt.assert_not_called()

    def test_backup_all_uses_state_plan_before_archive_targets(self):
        class FakeState:
            def current_gate(self, kuhb_id):
                return {"created": "create:completed", "absent": "linked", "dom0": "dom0"}[kuhb_id]

        ctx = SimpleNamespace(
            defaults={"backup": {"dom0_paths": ["/home/user/*"]}},
            logger=Mock(),
            state_store=FakeState(),
        )
        definitions = ({"id": "created", "order": 10}, {"id": "absent", "order": 20})

        with patch("kuhbs.cli.inspect_startup_config", return_value=SimpleNamespace(defaults={}, kuhb_definitions=definitions)):
            with patch("kuhbs.cli.make_context", return_value=ctx):
                with patch("kuhbs.cli.backup.backup_enabled_kuhs", return_value=[object()]):
                    with patch("kuhbs.cli.archive_targets.targets_for_all", return_value=["created-target", "dom0-target"]) as targets_for_all:
                        with patch("kuhbs.cli.backup.run_archive_targets") as run_archive_targets, \
                             patch("builtins.input") as prompt:
                            status, output = self.run_cli(["backup-all"])

        self.assertEqual(status, 0)
        self.assertIn("cannot run:\n  absent: state is linked", output)
        self.assertIn("will run:\n  created, dom0", output)
        self.assertEqual(targets_for_all.call_args.kwargs, {"include_dom0": True})
        self.assertEqual(targets_for_all.call_args.args[:2], (ctx, ({"id": "created", "order": 10},)))
        run_archive_targets.assert_called_once_with(ctx, ["created-target", "dom0-target"], source="backup-all")
        prompt.assert_not_called()

    def test_backup_all_fails_clearly_without_any_backup_configuration(self):
        # An empty shared plan must be nonzero instead of looking like a completed backup
        class FakeState:
            def current_gate(self, kuhb_id):
                return {"alpha": "create:completed", "dom0": "dom0"}[kuhb_id]

        ctx = SimpleNamespace(
            defaults={"backup": {"dom0_paths": []}},
            logger=Mock(),
            state_store=FakeState(),
        )
        definitions = ({"id": "alpha", "order": 10},)

        with patch("kuhbs.cli.inspect_startup_config", return_value=SimpleNamespace(defaults={}, kuhb_definitions=definitions)):
            with patch("kuhbs.cli.make_context", return_value=ctx):
                with patch("kuhbs.cli.backup.backup_enabled_kuhs", return_value=[]):
                    with patch("kuhbs.cli.archive_targets.targets_for_all") as targets_for_all:
                        with patch("kuhbs.cli.backup.run_archive_targets") as run_archive_targets:
                            status, output = self.run_cli(["backup-all"])

        self.assertEqual(status, 1)
        self.assertIn("No eligible targets", output)
        targets_for_all.assert_not_called()
        run_archive_targets.assert_not_called()

    def test_check_reports_every_broken_linked_kuhb_and_fails(self):
        broken = (
            BrokenKuhb("alpha", Path("/active/alpha/kuhb.yml"), (ConfigIssue(Path("/active/alpha/kuhb.yml"), "bad alpha"),)),
            BrokenKuhb("bravo", Path("/active/bravo/kuhb.yml"), (ConfigIssue(Path("/active/bravo/kuhb.yml"), "bad bravo"),)),
        )
        snapshot = SimpleNamespace(defaults={}, kuhb_definitions=(), broken_kuhbs=broken)

        with patch("kuhbs.cli.inspect_startup_config", return_value=snapshot):
            status, output = self.run_cli(["check"])

        self.assertEqual(status, 1)
        self.assertIn("alpha", output)
        self.assertIn("bad alpha", output)
        self.assertIn("bravo", output)
        self.assertIn("bad bravo", output)

    def test_targeted_valid_definition_runs_with_another_linked_kuhb_broken(self):
        class FakeState:
            def current_gate(self, kuhb_id):
                return "linked"

        valid = ({"id": "good", "order": 100},)
        issue = ConfigIssue(Path("/active/bad/kuhb.yml"), "invalid")
        broken = (BrokenKuhb("bad", issue.path, (issue,)),)
        snapshot = SimpleNamespace(defaults={}, kuhb_definitions=valid, broken_kuhbs=broken)
        ctx = SimpleNamespace(logger=Mock(), state_store=FakeState())

        with patch("kuhbs.cli.inspect_startup_config", return_value=snapshot), \
             patch("kuhbs.cli.make_context", return_value=ctx), \
             patch("kuhbs.cli.create.run_all") as run_all:
            status, output = self.run_cli(["create", "good", "bad"])

        self.assertEqual(status, 1)
        self.assertIn("bad: configuration broken", output)
        run_all.assert_not_called()

    def test_all_command_prints_broken_kuhb_and_dispatches_possible_siblings(self):
        valid = ({"id": "good", "order": 100},)
        issue = ConfigIssue(Path("/active/bad/kuhb.yml"), "invalid")
        snapshot = SimpleNamespace(defaults={}, kuhb_definitions=valid, broken_kuhbs=(BrokenKuhb("bad", issue.path, (issue,)),))
        ctx = SimpleNamespace(logger=Mock(), state_store=SimpleNamespace(current_gate=lambda _kuhb_id: "linked"))

        with patch("kuhbs.cli.inspect_startup_config", return_value=snapshot), \
             patch("kuhbs.cli.make_context", return_value=ctx), \
             patch("kuhbs.cli.create.run_all") as run_all:
            status, output = self.run_cli(["create-all"])

        self.assertEqual(status, 0)
        self.assertIn("bad", output)
        run_all.assert_called_once_with(ctx, valid)

    def test_backup_mount_remains_usable_with_broken_linked_kuhb(self):
        issue = ConfigIssue(Path("/active/bad/kuhb.yml"), "invalid")
        snapshot = SimpleNamespace(defaults={}, kuhb_definitions=(), broken_kuhbs=(BrokenKuhb("bad", issue.path, (issue,)),))
        ctx = SimpleNamespace(logger=Mock())

        with patch("kuhbs.cli.inspect_startup_config", return_value=snapshot), \
             patch("kuhbs.cli.make_context", return_value=ctx), \
             patch("kuhbs.cli.backup_mount.run_mount") as run_mount:
            status, _output = self.run_cli(["backup-mount"])

        self.assertEqual(status, 0)
        run_mount.assert_called_once_with(ctx)

    def test_list_renders_valid_status_then_reports_broken_entries(self):
        issue = ConfigIssue(Path("/active/bad/kuhb.yml"), "invalid")
        broken = (BrokenKuhb("bad", issue.path, (issue,)),)
        snapshot = SimpleNamespace(defaults={}, kuhb_definitions=({"id": "good"},), broken_kuhbs=broken)
        ctx = SimpleNamespace(logger=Mock())

        with patch("kuhbs.cli.inspect_startup_config", return_value=snapshot), \
             patch("kuhbs.cli.make_context", return_value=ctx), \
             patch("kuhbs.cli.print_list") as print_list:
            status, output = self.run_cli(["list"])

        self.assertEqual(status, 1)
        print_list.assert_called_once_with(ctx, snapshot.kuhb_definitions)
        self.assertIn("bad", output)
        self.assertIn("invalid", output)


if __name__ == "__main__":
    unittest.main()
