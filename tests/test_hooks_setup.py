# Purpose: Regression tests for hooks setup
# Scope: Uses temp dirs and fake runners instead of mutating real Qubes state
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from kuhbs.hooks import hook_paths, run_hook, run_script_in_kuh_terminal, setup_script_paths, setup_template_dir
from kuhbs.command import CommandResult, CommandRunner
from helpers import RecordingEventLogger as EventLogger
from kuhbs.model import merge_kuhb_definition
from tests.test_logging_commands_scripts import TtyStringIO
from kuhbs.operations import OperationContext
from kuhbs.operations import backup, create, upgrade, remove
from kuhbs.operations.archive_targets import ArchiveTarget
from helpers import MappingRunner, RunnerContext, current_defaults


def make_context(tmp_path):
    runner = tmp_path / "usr/share/kuhbs/setup-scripts/kuhbs/kuhbs-run-script.sh"
    runner.parent.mkdir(parents=True)
    # Tests provide the installed runner path explicitly; production no longer falls back to repo templates
    runner.write_text("#!/bin/bash\nexit 0\n")
    runner.chmod(0o700)
    apt_update = runner.parent / "setup/apt-update.sh"
    apt_update.parent.mkdir(parents=True)
    # Upgrade-hook tests provide the installed package script before exercising hook order
    apt_update.write_text("apt-get update\n", encoding="utf-8")
    defaults = {
        "paths": {
            "config": str(tmp_path / ".kuhbs"),
            "kuhbs": str(tmp_path / ".kuhbs/my-kuhbs"),
            "setup_scripts": str(tmp_path / "usr/share/kuhbs/setup-scripts"),
            "user_setup_scripts": str(tmp_path / ".kuhbs/setup-scripts"),
            "scripts": str(tmp_path / ".kuhbs/scripts"),
            "desktop_applications": str(tmp_path / ".local/share/applications"),
        },
        "terminal": {"path": "/usr/bin/xfce4-terminal", "args": [], "fallback": {"path": "/usr/bin/xterm", "args": []}},
        "backup": {"kuh": "ndp-kuhbs-usb", "crypt_name": "kuhbs-backups", "ignore_failed_read": True, "dom0_paths": ["/home/user/*"], "max_age_hours": 24},
        "upgrade": {"restart_without_prompt": []},
        "prefs": {"app": {"autostart": False}, "tpl": {"label": "purple"}, "sta": {"autostart": False}, "ndp": {"autostart": False}},
        "services": {"app": {}, "tpl": {}, "sta": {"instances": [{"id": "default"}]}, "ndp": {}},
        "features": {"app": {}, "tpl": {}, "sta": {"instances": [{"id": "default"}]}, "ndp": {}},
    }
    logger = EventLogger(log_path=tmp_path / "kuhbs.log", color=False, stdout=False)
    # Create fixtures model both managed VMs as absent before the preflight runs
    fake_runner = MappingRunner(
        logger,
        returncodes={
            ("qvm-check", "--quiet", "app-signal"): 1,
            ("qvm-check", "--quiet", "tpl-signal"): 1,
        },
    )
    return RunnerContext(current_defaults(defaults), logger, fake_runner)


def mark_managed_kuhs_existing(ctx: RunnerContext) -> None:
    # Hook tests for non-create operations begin from already-created VM resources
    for name in ("tpl-signal", "app-signal"):
        ctx.runner.returncodes[("qvm-check", "--quiet", name)] = 0


def make_definition(tmp_path):
    kuhb_dir = tmp_path / ".kuhbs/my-kuhbs/signal"
    (kuhb_dir / "hooks/app").mkdir(parents=True)
    (kuhb_dir / "hooks/app/create-pre.sh").write_text("echo app pre\n")
    (kuhb_dir / "hooks/app/create-pre.sh").chmod(0o700)
    (kuhb_dir / "hooks/app/create-post.sh").write_text("echo app post\n")
    (kuhb_dir / "hooks/app/create-post.sh").chmod(0o700)
    (kuhb_dir / "hooks/app/remove.sh").write_text("echo app remove\n")
    (kuhb_dir / "hooks/app/remove.sh").chmod(0o700)
    (kuhb_dir / "hooks/app/update.sh").write_text("echo app update\n")
    (kuhb_dir / "hooks/app/update.sh").chmod(0o700)
    (kuhb_dir / "hooks/app/backup.sh").write_text("echo app backup\n")
    (kuhb_dir / "hooks/app/backup.sh").chmod(0o700)
    (kuhb_dir / "templates/app").mkdir(parents=True)
    (kuhb_dir / "scripts/app").mkdir(parents=True)
    (kuhb_dir / "scripts/app/10-local.sh").write_text("echo local app setup\n")
    (kuhb_dir / "templates/app/local.conf").write_text("local template\n")
    (kuhb_dir / "templates/app/override.conf").write_text("local override\n")
    (kuhb_dir / "templates/app/.local-dotfile").write_text("local dot template\n")
    shared = tmp_path / "usr/share/kuhbs/setup-scripts/app/shared.sh"
    shared.parent.mkdir(parents=True)
    shared.write_text("echo shared app setup\n")
    (tmp_path / "usr/share/kuhbs/setup-scripts/kuhbs/setup/templates").mkdir(parents=True)
    (tmp_path / "usr/share/kuhbs/setup-scripts/kuhbs/setup/templates/common.conf").write_text("common template\n")
    (tmp_path / "usr/share/kuhbs/setup-scripts/kuhbs/setup/templates/override.conf").write_text("common override\n")
    (tmp_path / "usr/share/kuhbs/setup-scripts/kuhbs/setup/templates/.common-dotfile").write_text("common dot template\n")
    return {
        "id": "signal",
        "name": "Signal",
        "description": "Signal is a private messenger kuhb",

        "template": "debian-13-minimal",
        "kuhs": {"tpl": {}, "app": {"instances": [{"id": "default", "prefs": {"label": "green"}}], "backup": {"paths": ["/home/user/.config/Signal"]}}},
        "setup_scripts": {"app": ["app/shared.sh"]},
    }


class FakeTerminalProcess:
    def __init__(self, exit_after_polls: int, returncode: int = 0):
        self.exit_after_polls = exit_after_polls
        self.final_returncode = returncode
        self.polls = 0
        self.returncode = None

    def poll(self):
        self.polls += 1
        if self.polls >= self.exit_after_polls:
            self.returncode = self.final_returncode
            return self.final_returncode
        return None

    def communicate(self):
        self.returncode = self.final_returncode
        return "", ""


class PollingScriptRunner(CommandRunner):
    def __init__(self, logger, *, script_exit=0, terminal_exit_after_polls=2, terminal_returncode=0):
        super().__init__(logger)
        self.script_exit = script_exit
        self.process = FakeTerminalProcess(terminal_exit_after_polls, terminal_returncode)
        self.finished = False

    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        if log:
            self.logger.command(source, args)
        returncode = 0
        if args[:3] == ["qvm-run", "--quiet", "app-signal"] and args[-2:] == ["-x", "/usr/bin/xfce4-terminal"]:
            returncode = 0
        elif args[:5] == ["qvm-run", "--quiet", "--user", "root", "app-signal"] and args[5] == "test":
            returncode = 0
        elif args[:5] == ["qvm-run", "--quiet", "--user", "root", "app-signal"] and args[5].startswith("/tmp/.kuhbs-script-exit-status-"):
            returncode = self.script_exit
        if log:
            self.logger.exit(source, returncode, " ".join(args))
        if check and returncode != 0:
            raise RuntimeError("unexpected checked command")
        return CommandResult(returncode=returncode)

    def start(self, args, *, source="dom0"):
        self.logger.command(source, args)
        return self.process

    def poll(self, process):
        return process.poll()

    def finish(self, process, args, *, source="dom0", check=True, log=True):
        process.communicate()
        self.finished = True
        if log:
            self.logger.exit(source, process.returncode, " ".join(args))
        return CommandResult(returncode=process.returncode)


class PollingScriptContext(OperationContext):
    def __init__(self, defaults, logger, runner):
        super().__init__(defaults, logger)
        self._runner = runner

    @property
    def runner(self):
        return self._runner


class HookAndSetupTests(unittest.TestCase):
    def test_setup_templates_use_only_templates_kind(self):
        # KUHB setup payloads have one path contract: <kuhb>/templates/<kind>
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            canonical = root / "templates/app"
            canonical.mkdir(parents=True)

            self.assertEqual(setup_template_dir(root, "app"), canonical)

    def test_shared_runner_prompts_for_debug_shell_on_success_and_failure(self):
        runner = Path("install/templates/usr/share/kuhbs/setup-scripts/kuhbs/kuhbs-run-script.sh").read_text()

        self.assertIn("success_color=$'\\033[32m'", runner)
        self.assertIn("failure_color=$'\\033[31m'", runner)
        self.assertIn("reset_color=$'\\033[0m'", runner)
        self.assertIn("Exit status 0. This terminal will close automatically in 5 seconds. Press ENTER now to open a shell.", runner)
        self.assertIn("${failure_color}\\n\\nScript failed with exit code $run_exit_status${reset_color}", runner)
        self.assertIn("read -r -t 5", runner)
        self.assertIn("read -r -p \"Press ENTER to start a shell, or CTRL + C to close this terminal\"", runner)
        self.assertIn("printf \"%b\" \"$reset_color\"\n        /bin/bash -i", runner)

    def test_hook_paths_discovers_type_and_instance_hooks_in_order(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "hooks/sta").mkdir(parents=True)
            (root / "hooks/sta/private").mkdir(parents=True)
            (root / "hooks/sta/create-pre.sh").write_text("echo type\n")
            (root / "hooks/sta/private/create-pre.sh").write_text("echo instance\n")

            paths = hook_paths(root, "sta", "private", "create-pre")

            self.assertEqual(paths, [root / "hooks/sta/create-pre.sh", root / "hooks/sta/private/create-pre.sh"])

    def test_setup_script_paths_accept_absolute_packaged_and_custom_paths(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            definition = make_definition(tmp_path)
            root = tmp_path / ".kuhbs/my-kuhbs" / definition["id"]
            kuh = SimpleNamespace(kind="app", config={"setup_scripts": ["/usr/share/kuhbs/setup-scripts/kuhbs/setup/basics.sh", "~/.kuhbs/setup-scripts/user.sh"]})

            paths = setup_script_paths(root, kuh)

            self.assertIn(Path("/usr/share/kuhbs/setup-scripts/kuhbs/setup/basics.sh"), paths)
            self.assertIn(Path.home() / ".kuhbs/setup-scripts/user.sh", paths)

    def test_run_hook_copies_to_vm_and_runs_with_visible_cli_output(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = make_context(tmp_path)
            script = tmp_path / "hook.sh"
            script.write_text("echo hook\n")

            run_hook(ctx, "signal", "app-signal", script)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("qvm-copy-to-vm app-signal", log_text)
            self.assertIn("kuhbs-run-script.sh", log_text)
            self.assertIn("/home/user/QubesIncoming/dom0/hook.sh", log_text)
            self.assertIn("qvm-run --quiet --user root app-signal /tmp/.kuhbs-script-exit-status-", log_text)
            self.assertNotIn("OUTPUT", log_text)

    def test_vm_script_success_waits_for_terminal_process_after_status_file(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx = make_context(tmp_path)
            base_ctx.logger.stdout = True
            runner = PollingScriptRunner(base_ctx.logger, script_exit=0, terminal_exit_after_polls=1)
            ctx = PollingScriptContext(base_ctx.defaults, base_ctx.logger, runner)
            runner_path = tmp_path / "usr/share/kuhbs/setup-scripts/kuhbs/kuhbs-run-script.sh"
            runner_path.parent.mkdir(parents=True, exist_ok=True)
            runner_path.write_text("echo runner\n")
            script = tmp_path / "hook.sh"
            script.write_text("echo hook\n")
            tty = TtyStringIO()

            with patch("sys.stdout", tty):
                run_script_in_kuh_terminal(ctx, "signal", "app-signal", script)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("Waiting for the script to complete by executing qvm-run --quiet --user root app-signal test -f /tmp/.kuhbs-script-exit-status-", log_text)
            self.assertIn("once a second, to determine if the script has completed yet", log_text)
            self.assertNotIn("COMMAND   dom0                      qvm-run --quiet --user root app-signal test -f", log_text)
            self.assertIn("EXIT      app-signal                0", log_text)
            self.assertIn("rm -f /home/user/QubesIncoming/dom0/kuhbs-run-script.sh /home/user/QubesIncoming/dom0/hook.sh /tmp/.kuhbs-script-exit-status-", log_text)
            self.assertNotIn("\nEXIT", tty.getvalue())
            self.assertTrue(runner.finished)

    def test_vm_script_success_uses_terminal_qvm_run_exit_status(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx = make_context(tmp_path)
            runner = PollingScriptRunner(base_ctx.logger, script_exit=0, terminal_exit_after_polls=1, terminal_returncode=9)
            ctx = PollingScriptContext(base_ctx.defaults, base_ctx.logger, runner)
            runner_path = tmp_path / "usr/share/kuhbs/setup-scripts/kuhbs/kuhbs-run-script.sh"
            runner_path.parent.mkdir(parents=True, exist_ok=True)
            runner_path.write_text("echo runner\n")
            script = tmp_path / "hook.sh"
            script.write_text("echo hook\n")

            with self.assertRaisesRegex(RuntimeError, "script terminal failed with exit status 9"):
                run_script_in_kuh_terminal(ctx, "signal", "app-signal", script)

            self.assertTrue(runner.finished)

    def test_vm_script_failure_waits_for_debug_terminal_to_close(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx = make_context(tmp_path)
            runner = PollingScriptRunner(base_ctx.logger, script_exit=7, terminal_exit_after_polls=2)
            ctx = PollingScriptContext(base_ctx.defaults, base_ctx.logger, runner)
            runner_path = tmp_path / "usr/share/kuhbs/setup-scripts/kuhbs/kuhbs-run-script.sh"
            runner_path.parent.mkdir(parents=True, exist_ok=True)
            runner_path.write_text("echo runner\n")
            script = tmp_path / "hook.sh"
            script.write_text("echo hook\n")

            with patch("kuhbs.hooks.time.sleep"), self.assertRaisesRegex(RuntimeError, r"failed with exit status 7: app-signal:.*/hook\.sh"):
                run_script_in_kuh_terminal(ctx, "signal", "app-signal", script)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("EXIT      app-signal                7", log_text)
            self.assertTrue(runner.finished)

    def test_create_runs_create_hooks_in_dom0_and_setup_scripts_in_vm_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = make_context(tmp_path)
            definition = merge_kuhb_definition(ctx.defaults, make_definition(tmp_path))

            create.run(ctx, definition)

            log_text = (tmp_path / "kuhbs.log").read_text()
            hook_path = str(ctx.kuhbs_root / definition["id"] / "hooks/app/create-pre.sh")
            # The dedicated dom0 terminal command must be recorded before VM setup begins
            self.assertLess(log_text.index(hook_path), log_text.index("setup-app-signal.sh"))
            # Setup artifacts are now exercised through a fake runner instead of suppressing filesystem behavior
            self.assertTrue((tmp_path / ".kuhbs/scripts/signal/setup-app-signal.sh").is_file())
            self.assertIn("qvm-copy-to-vm app-signal " + str(tmp_path / ".kuhbs/template-copies/app-signal/templates"), log_text)
            self.assertFalse((tmp_path / ".kuhbs/template-copies/app-signal").exists())
            post_hook_path = str(ctx.kuhbs_root / definition["id"] / "hooks/app/create-post.sh")
            self.assertLess(log_text.index("setup-app-signal.sh"), log_text.index(post_hook_path))
            self.assertIn("rm -rf /home/user/QubesIncoming/", log_text)

    def test_upgrade_runs_update_hook(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = make_context(tmp_path)
            definition = merge_kuhb_definition(ctx.defaults, make_definition(tmp_path))
            mark_managed_kuhs_existing(ctx)

            upgrade.run(ctx, definition)

            self.assertIn("qvm-copy-to-vm app-signal", (tmp_path / "kuhbs.log").read_text())
            self.assertIn("kuhbs-run-script.sh", (tmp_path / "kuhbs.log").read_text())
            self.assertIn("/home/user/QubesIncoming/dom0/update.sh", (tmp_path / "kuhbs.log").read_text())

    def test_remove_runs_remove_hook_before_qvm_remove(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = make_context(tmp_path)
            definition = merge_kuhb_definition(ctx.defaults, make_definition(tmp_path))
            mark_managed_kuhs_existing(ctx)
            ctx.set_state("signal", "create", "completed")

            # This test owns hook ordering, not optional Snitch policy deletion
            with patch("builtins.input", return_value="n"):
                remove.run(ctx, definition)

            log_text = (tmp_path / "kuhbs.log").read_text()
            hook_path = str(ctx.kuhbs_root / definition["id"] / "hooks/app/remove.sh")
            # Hook completion still gates destructive removal even though the hook now owns a separate terminal
            self.assertLess(log_text.index(hook_path), log_text.index("qvm-remove --force app-signal"))

    def test_backup_runs_backup_hook_in_vm_before_archive(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = make_context(tmp_path)
            definition = merge_kuhb_definition(ctx.defaults, make_definition(tmp_path))
            mark_managed_kuhs_existing(ctx)
            ctx.set_state("signal", "create", "completed")

            target = ArchiveTarget("app-signal", "signal", definition, backup.backup_enabled_kuhs(ctx, definition)[0])
            backup.run_archive_targets(ctx, [target])

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertLess(log_text.index("/home/user/QubesIncoming/dom0/backup.sh"), log_text.index("Creating archive for app-signal"))
            self.assertIn("zstd -T0 -3", log_text)
            self.assertIn("-cpf - --ignore-failed-read -- /home/user/.config/Signal", log_text)
            self.assertIn("kuhbs-run-script.sh", log_text)
            self.assertIn("qvm-copy-to-vm app-signal", log_text)


if __name__ == "__main__":
    unittest.main()
