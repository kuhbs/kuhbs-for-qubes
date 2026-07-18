# Purpose: Regression tests for terminal resolution
# Scope: Uses temp dirs and fake runners instead of mutating real Qubes state
from pathlib import Path
from shlex import split
import os
import subprocess
import tempfile
from threading import Barrier, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kuhbs.command import CommandResult, CommandRunner
from kuhbs.hooks import _wait_for_terminal_script_status, run_hook, run_setup_scripts
from helpers import MappingRunner, RecordingEventLogger as EventLogger, RunnerContext
from kuhbs.model import Kuh
from kuhbs.operations import OperationContext
from kuhbs.operations import terminal
from kuhbs.terminal import terminal_command_prefix
from helpers import current_defaults


def context(tmp_path):
    runner = tmp_path / "usr/share/kuhbs/setup-scripts/kuhbs/kuhbs-run-script.sh"
    runner.parent.mkdir(parents=True)
    # Terminal tests install the runner fixture exactly where defaults.yml points
    runner.write_text("#!/bin/bash\nexit 0\n")
    runner.chmod(0o700)
    defaults = {
        "paths": {"config": str(tmp_path / ".kuhbs"), "kuhbs": str(tmp_path), "scripts": str(tmp_path / ".kuhbs/scripts"), "setup_scripts": str(tmp_path / "usr/share/kuhbs/setup-scripts"), "user_setup_scripts": str(tmp_path / ".kuhbs/setup-scripts")},
        "terminal": {
            "path": "/usr/bin/xfce4-terminal",
            "args": ["--hide-menubar", "--hide-borders"],
            "fallback": {"path": "/usr/bin/xterm", "args": ["-bg", "black"]},
        },
    }
    logger = EventLogger(log_path=tmp_path / "kuhbs.log", color=False, stdout=False)
    return RunnerContext(current_defaults(defaults), logger, MappingRunner(logger))


class MissingTerminalVmRunner(CommandRunner):
    def __init__(self, logger):
        self.logger = logger

    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        if log:
            self.logger.command(source, args)
        returncode = 1 if args == ["qvm-check", "--quiet", "app-missing"] else 0
        if log:
            self.logger.exit(source, returncode, " ".join(args))
        if check and returncode != 0:
            raise RuntimeError("unexpected checked missing VM")
        return CommandResult(returncode=returncode)


class MissingTerminalVmContext(OperationContext):
    @property
    def runner(self):
        return MissingTerminalVmRunner(self.logger)


class FallbackTerminalContext:
    def __init__(self, defaults, logger):
        self.defaults = defaults
        self.logger = logger
        self.runner = FallbackTerminalRunner(logger)
        self.kuhbs_root = Path(defaults["paths"]["kuhbs"])


class FallbackTerminalRunner:
    def __init__(self, logger):
        self.logger = logger

    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        if log:
            self.logger.command(source, args)
        if args == ["qvm-run", "--quiet", "tpl-signal2", "test", "-x", "/usr/bin/xfce4-terminal"]:
            if log:
                self.logger.exit(source, 1, " ".join(args))
            return CommandResult(returncode=1)
        if log:
            self.logger.exit(source, 0, " ".join(args))
        return CommandResult(returncode=0)

    def run_for_kuh(self, kuh_name, args, *, check=True, visible_output=False, log=True):
        return self.run(args, source=kuh_name, check=check, visible_output=visible_output, log=log)

    def start(self, args, *, source="dom0"):
        self.logger.command(source, args)
        return None

    def poll(self, process):
        return 0

    def finish(self, process, args, *, source="dom0", check=True, log=True):
        if log:
            self.logger.exit(source, 0, " ".join(args))
        return CommandResult(returncode=0)


class PollingRunner:
    def __init__(self, logger):
        self.logger = logger
        self.polls = 0
        self.status_checks = 0

    def start(self, args, *, source="dom0"):
        self.logger.command(source, args)
        return object()

    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        if log:
            self.logger.command(source, args)
        if args[5:7] == ["test", "-f"]:
            self.status_checks += 1
            returncode = 0 if self.status_checks == 2 else 1
        else:
            returncode = 0
        if log:
            self.logger.exit(source, returncode, " ".join(args))
        return CommandResult(returncode=returncode)

    def poll(self, process):
        self.polls += 1
        return 0 if self.polls == 3 else None

    def finish(self, process, args, *, source="dom0", check=True, log=True):
        if log:
            self.logger.exit(source, 0, " ".join(args))
        return CommandResult(returncode=0)

    def run_for_kuh(self, kuh_name, args, *, check=True, visible_output=False, log=True):
        return self.run(args, source=kuh_name, check=check, visible_output=visible_output, log=log)


class Dom0HookStatusRunner(MappingRunner):
    # A real terminal can outlive the hook status marker while the user reads output or uses a debug shell
    def __init__(self, logger, status_file: str, *, script_returncode: int, status_exists: bool = True):
        super().__init__(logger)
        self.status_file = status_file
        self.script_returncode = script_returncode
        self.status_exists = status_exists
        self.polls = 0

    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        if args == ["test", "-f", self.status_file]:
            self.commands.append(list(args))
            return CommandResult(returncode=0 if self.status_exists else 1)
        if args == [self.status_file]:
            self.commands.append(list(args))
            return CommandResult(returncode=self.script_returncode)
        return super().run(args, source=source, check=check, visible_output=visible_output, log=log)

    def poll(self, process):
        self.polls += 1
        return 0 if self.polls == 2 else None


class Dom0HookClosingRaceRunner(Dom0HookStatusRunner):
    # The terminal exits after the marker check, while its wrapper publishes the marker before the final recheck
    def __init__(self, logger, status_file: str):
        super().__init__(logger, status_file, script_returncode=0, status_exists=False)
        self.status_checks = 0

    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        if args == ["test", "-f", self.status_file]:
            self.commands.append(list(args))
            self.status_checks += 1
            return CommandResult(returncode=0 if self.status_checks == 2 else 1)
        return super().run(args, source=source, check=check, visible_output=visible_output, log=log)

    def poll(self, process):
        return 0


class InterruptibleDom0HookRunner(Dom0HookStatusRunner):
    # Direct single-hook Ctrl+C must kill and reap the tracked terminal instead of relying on batch cleanup
    def __init__(self, logger, status_file: str):
        super().__init__(logger, status_file, script_returncode=0, status_exists=False)
        self.cleanup = []

    def poll(self, process):
        return None

    def kill_active_processes(self):
        self.cleanup.append("kill")

    def finish(self, process, args, *, source="dom0", check=True, log=True):
        self.cleanup.append("finish")
        return CommandResult(returncode=-9)


class ParallelDom0HookRunner(MappingRunner):
    # Both workers must own a terminal before either can finish, proving all-mode hooks are not serialized
    def __init__(self, logger):
        super().__init__(logger)
        self.started = Barrier(2)

    def start(self, args, *, source="dom0", detached=False):
        process = super().start(args, source=source, detached=detached)
        self.started.wait(timeout=2)
        return process

    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        if args[:2] == ["test", "-f"] and args[-1].startswith("/tmp/.kuhbs-script-exit-status-"):
            self.commands.append(list(args))
            return CommandResult(returncode=0)
        if len(args) == 1 and args[0].startswith("/tmp/.kuhbs-script-exit-status-"):
            self.commands.append(list(args))
            return CommandResult(returncode=0)
        return super().run(args, source=source, check=check, visible_output=visible_output, log=log)


class PollingContext:
    def __init__(self, logger):
        self.logger = logger
        self.runner = PollingRunner(logger)


class TerminalResolutionTests(unittest.TestCase):
    def test_terminal_command_prefix_checks_preferred_terminal_and_uses_configured_args(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)

            prefix = terminal_command_prefix(ctx, "app-signal")

            self.assertEqual(prefix, ["/usr/bin/xfce4-terminal", "--hide-menubar", "--hide-borders"])
            self.assertIn("qvm-run --quiet app-signal test -x /usr/bin/xfce4-terminal", (tmp_path / "kuhbs.log").read_text())

    def test_terminal_operation_uses_resolved_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)

            terminal.run(ctx, "app-signal")

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("qvm-run --quiet app-signal test -x /usr/bin/xfce4-terminal", log_text)
            self.assertIn("qvm-run --quiet --user user app-signal /usr/bin/xfce4-terminal", log_text)
            self.assertNotIn("EXIT      app-signal                qvm-run --quiet --user user app-signal /usr/bin/xfce4-terminal", log_text)
            self.assertIn("app-signal: Terminal", log_text)

    def test_terminal_operation_aborts_before_terminal_probe_when_vm_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx = context(tmp_path)
            ctx = MissingTerminalVmContext(base_ctx.defaults, base_ctx.logger)

            with self.assertRaisesRegex(RuntimeError, "VM does not exist: app-missing"):
                terminal.run(ctx, "app-missing")

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("qvm-check --quiet app-missing", log_text)
            self.assertNotIn("test -x", log_text)

    def test_terminal_operation_forwards_custom_user_to_qvm_run(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)

            terminal.run(ctx, "app-signal", user="root")

            # User existence is a guest concern; KUHBS forwards the name and lets qvm-run fail if needed
            self.assertIn("qvm-run --quiet --user root app-signal /usr/bin/xfce4-terminal", (tmp_path / "kuhbs.log").read_text())

    def test_terminal_operation_launches_an_unnamed_dispvm(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)

            terminal.run(ctx, "app-signal", dispvm=True)

            self.assertIn("qvm-run --quiet --user user --dispvm app-signal /usr/bin/xfce4-terminal", (tmp_path / "kuhbs.log").read_text())

    def test_dispvm_terminal_does_not_probe_persistent_appvm(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)

            terminal.run(ctx, "app-signal", dispvm=True)

            self.assertNotIn(
                "qvm-run --quiet app-signal test -x",
                (tmp_path / "kuhbs.log").read_text(),
            )

    def test_terminal_operation_shuts_down_after_clean_terminal_exit(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)

            terminal.run(ctx, "app-signal", shutdown_on_exit_0=True)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("qvm-run --quiet --user user app-signal /usr/bin/bash -lc", log_text)
            self.assertIn("/usr/bin/xfce4-terminal --disable-server", log_text)
            self.assertIn("&& /usr/bin/sudo --non-interactive /sbin/shutdown -h now", log_text)

    def test_terminal_operation_opens_dom0_terminal_without_qvm_run(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)

            with patch("kuhbs.operations.terminal.getpass.getuser", return_value="user"), \
                    patch.object(ctx.runner, "start", return_value=None) as start:
                terminal.run(ctx, "dom0")

            command = start.call_args.args[0]
            self.assertEqual(start.call_args.kwargs, {"source": "dom0", "detached": True})
            self.assertIn("/usr/bin/xfce4-terminal", command)
            self.assertIn("dom0: Terminal", command)
            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("Opening terminal in dom0", log_text)
            self.assertNotIn("qvm-run", log_text)

    def test_terminal_operation_opens_dom0_terminal_as_custom_user(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)

            with patch("kuhbs.operations.terminal.getpass.getuser", return_value="user"), \
                    patch.object(ctx.runner, "start", return_value=None) as start:
                terminal.run(ctx, "dom0", user="root")

            command = start.call_args.args[0]
            self.assertEqual(start.call_args.kwargs, {"source": "dom0", "detached": True})
            self.assertEqual(command[:5], ["sudo", "--user", "root", "--", "/usr/bin/xfce4-terminal"])
            self.assertIn("dom0: Terminal", command)
            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("Opening terminal in dom0", log_text)
            self.assertNotIn("qvm-run", log_text)

    def test_script_wrapper_writes_directly_executable_status_marker(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            wrapper = Path(__file__).resolve().parents[1] / "install/templates/usr/share/kuhbs/setup-scripts/kuhbs/kuhbs-run-script.sh"
            script = tmp_path / "fails.sh"
            status = tmp_path / "status"
            witness = tmp_path / "published-before-chmod"
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            script.write_text("exit 7\n", encoding="utf-8")
            # A fake chmod records whether the poll-visible final marker existed before it became executable
            fake_chmod = fake_bin / "chmod"
            fake_chmod.write_text(
                "#!/bin/bash\n"
                "if [[ -e \"$FINAL_STATUS\" ]]; then touch \"$WITNESS\"; fi\n"
                "exec /usr/bin/chmod \"$@\"\n",
                encoding="utf-8",
            )
            fake_chmod.chmod(0o700)
            env = dict(os.environ)
            env.update({
                "PATH": f"{fake_bin}:{env['PATH']}",
                "FINAL_STATUS": str(status),
                "WITNESS": str(witness),
            })

            subprocess.run(
                ["bash", str(wrapper), str(status), str(script)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                env=env,
            )
            try:
                result = subprocess.run([str(status)], check=False)
            except OSError as exc:
                self.fail(f"status marker is not directly executable: {exc}")

            self.assertFalse(witness.exists())
            self.assertEqual(result.returncode, 7)

    def test_dom0_hooks_run_in_tracked_interactive_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            script = tmp_path / "remove.sh"
            script.write_text("read -r answer\n", encoding="utf-8")
            status_file = "/tmp/.kuhbs-script-exit-status-dom0-test"
            ctx._runner = Dom0HookStatusRunner(ctx.logger, status_file, script_returncode=0)

            with patch("kuhbs.hooks.secrets.token_hex", return_value="dom0-test"), \
                    patch("kuhbs.hooks.time.sleep"):
                run_hook(ctx, "signal", "app-signal", script, location="dom0")

            terminals = [command for command in ctx.runner.commands if command and command[0] == "/usr/bin/xfce4-terminal"]
            self.assertEqual(len(terminals), 1)
            terminal = terminals[0]
            self.assertIn("--disable-server", terminal)
            self.assertIn("KUHBS: dom0 app-signal remove", terminal)
            command_text = terminal[terminal.index("--command") + 1]
            self.assertIn(str(tmp_path / "usr/share/kuhbs/setup-scripts/kuhbs/kuhbs-run-script.sh"), command_text)
            self.assertIn(status_file, command_text)
            self.assertIn(str(script), command_text)
            self.assertFalse(any(command and command[0] == "qvm-run" for command in ctx.runner.commands))
            self.assertEqual(ctx.runner.commands.count(["rm", "-f", status_file]), 2)
            self.assertIn(f"Executing hook {script}", (tmp_path / "kuhbs.log").read_text(encoding="utf-8"))

    def test_parallel_dom0_hooks_get_independent_terminals_and_status_files(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            ctx._runner = ParallelDom0HookRunner(ctx.logger)
            scripts = [tmp_path / "alpha.sh", tmp_path / "beta.sh"]
            for script in scripts:
                script.write_text("read -r answer\n", encoding="utf-8")
            errors = []

            def run_one(index):
                try:
                    run_hook(ctx, scripts[index].stem, f"app-{scripts[index].stem}", scripts[index], location="dom0")
                except BaseException as exc:
                    errors.append(exc)

            workers = [Thread(target=run_one, args=(index,)) for index in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=3)

            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(errors, [])
            terminals = [command for command in ctx.runner.commands if command and command[0] == "/usr/bin/xfce4-terminal"]
            self.assertEqual(len(terminals), 2)
            markers = {
                argument
                for terminal in terminals
                for argument in split(terminal[terminal.index("--command") + 1])
                if argument.startswith("/tmp/.kuhbs-script-exit-status-")
            }
            self.assertEqual(len(markers), 2)

    def test_failed_dom0_hook_waits_for_terminal_and_preserves_status(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            script = tmp_path / "update.sh"
            script.write_text("exit 7\n", encoding="utf-8")
            status_file = "/tmp/.kuhbs-script-exit-status-dom0-failed"
            ctx._runner = Dom0HookStatusRunner(ctx.logger, status_file, script_returncode=7)

            with patch("kuhbs.hooks.secrets.token_hex", return_value="dom0-failed"), \
                    patch("kuhbs.hooks.time.sleep"):
                with self.assertRaisesRegex(RuntimeError, "failed with exit status 7: dom0:.*update.sh"):
                    run_hook(ctx, "signal", "app-signal", script, location="dom0")

            self.assertEqual(ctx.runner.polls, 2)
            self.assertEqual(ctx.runner.commands.count(["rm", "-f", status_file]), 1)

    def test_dom0_hook_rechecks_status_when_terminal_exits(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            script = tmp_path / "create-post.sh"
            script.write_text("exit 0\n", encoding="utf-8")
            status_file = "/tmp/.kuhbs-script-exit-status-dom0-closing"
            ctx._runner = Dom0HookClosingRaceRunner(ctx.logger, status_file)

            with patch("kuhbs.hooks.secrets.token_hex", return_value="dom0-closing"), \
                    patch("kuhbs.hooks.time.sleep"):
                run_hook(ctx, "signal", "app-signal", script, location="dom0")

            self.assertEqual(ctx.runner.status_checks, 2)
            self.assertEqual(ctx.runner.commands.count(["rm", "-f", status_file]), 2)

    def test_dom0_hook_interrupt_kills_and_reaps_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            script = tmp_path / "remove.sh"
            script.write_text("read -r answer\n", encoding="utf-8")
            status_file = "/tmp/.kuhbs-script-exit-status-dom0-interrupt"
            ctx._runner = InterruptibleDom0HookRunner(ctx.logger, status_file)

            with patch("kuhbs.hooks.secrets.token_hex", return_value="dom0-interrupt"), \
                    patch("kuhbs.hooks.time.sleep", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    run_hook(ctx, "signal", "app-signal", script, location="dom0")

            self.assertEqual(ctx.runner.cleanup, ["kill", "finish"])

    def test_real_runner_interrupt_leaves_no_registered_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            logger = EventLogger(log_path=tmp_path / "kuhbs.log", color=False, stdout=False)
            runner = CommandRunner(logger)
            ctx = SimpleNamespace(runner=runner, logger=logger)
            started = []
            original_start = runner.start

            def record_start(args, *, source="dom0", detached=False):
                process = original_start(args, source=source, detached=detached)
                started.append(process)
                return process

            with patch.object(runner, "start", side_effect=record_start), \
                    patch("kuhbs.hooks.time.sleep", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    _wait_for_terminal_script_status(
                        ctx,
                        "app-signal",
                        tmp_path / "remove.sh",
                        ["bash", "-c", "sleep 60"],
                        str(tmp_path / "missing-status"),
                        status_location="dom0",
                    )

            self.assertEqual(len(runner._active_processes), 0)
            self.assertEqual(len(started), 1)
            self.assertIsNotNone(started[0].poll())

    def test_dom0_hook_terminal_closing_before_status_fails(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            script = tmp_path / "backup.sh"
            script.write_text("echo backup\n", encoding="utf-8")
            status_file = "/tmp/.kuhbs-script-exit-status-dom0-missing"
            ctx._runner = Dom0HookStatusRunner(ctx.logger, status_file, script_returncode=0, status_exists=False)

            with patch("kuhbs.hooks.secrets.token_hex", return_value="dom0-missing"), \
                    patch("kuhbs.hooks.time.sleep"):
                with self.assertRaisesRegex(RuntimeError, "terminal closed before writing exit status: dom0:.*backup.sh"):
                    run_hook(ctx, "signal", "app-signal", script, location="dom0")

    def test_vm_hooks_run_via_terminal_script_wrapper(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            script = tmp_path / "hook.sh"
            script.write_text("echo hook\n")

            run_hook(ctx, "signal", "app-signal", script)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("qvm-copy-to-vm app-signal", log_text)
            self.assertIn("kuhbs-run-script.sh", log_text)
            self.assertIn("/usr/bin/xfce4-terminal --disable-server", log_text)
            self.assertIn("/home/user/QubesIncoming/dom0/hook.sh", log_text)
            self.assertIn("qvm-run --quiet --user root app-signal /tmp/.kuhbs-script-exit-status-", log_text)

    def test_script_status_polling_logs_one_wait_message_without_poll_spam(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            logger = EventLogger(log_path=tmp_path / "kuhbs.log", color=False, stdout=False)
            ctx = PollingContext(logger)

            with patch("kuhbs.hooks.time.sleep"):
                _wait_for_terminal_script_status(
                    ctx,
                    "app-signal",
                    tmp_path / "hook.sh",
                    ["qvm-run", "--quiet", "--user", "root", "app-signal", "terminal"],
                    "/tmp/.kuhbs-script-exit-status-test",
                )

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("Waiting for the script to complete by executing qvm-run --quiet --user root app-signal test -f /tmp/.kuhbs-script-exit-status-test once a second, to determine if the script has completed yet", log_text)
            self.assertNotIn("COMMAND   dom0                      qvm-run --quiet --user root app-signal test -f", log_text)
            self.assertNotIn("EXIT      dom0                      1", log_text)
            self.assertIn("COMMAND   app-signal                qvm-run --quiet --user root app-signal /tmp/.kuhbs-script-exit-status-test", log_text)

    def test_xterm_fallback_uses_wrapper_without_hold(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx = context(tmp_path)
            ctx = FallbackTerminalContext(base_ctx.defaults, base_ctx.logger)
            kuhb_dir = base_ctx.kuhbs_root / "signal2"
            (kuhb_dir / "scripts/tpl").mkdir(parents=True)
            (kuhb_dir / "scripts/tpl/10-setup.sh").write_text("echo setup\n")
            definition = {"id": "signal2"}
            kuh = Kuh("signal2", "tpl", "tpl-signal2", None, {})

            run_setup_scripts(ctx, definition, kuh)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("WARNING   tpl-signal2", log_text)
            self.assertIn("/usr/bin/xfce4-terminal not found, falling back to /usr/bin/xterm", log_text)
            self.assertIn("/usr/bin/xterm -bg black -T 'KUHBS: tpl-signal2 setup-tpl-signal2' -e /home/user/QubesIncoming/dom0/kuhbs-run-script.sh", log_text)


if __name__ == "__main__":
    unittest.main()
