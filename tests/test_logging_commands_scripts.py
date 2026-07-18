# Purpose: Regression tests for logging commands scripts
# Scope: Uses temp dirs and fake runners instead of mutating real Qubes state
from io import StringIO
import os
from pathlib import Path
import subprocess
from threading import Event, Thread
import tempfile
import unittest
from unittest.mock import patch

from kuhbs.log import EventLogger as TerminalEventLogger
from kuhbs.command import CommandRunner
from kuhbs.scripts import concatenate_script_fragments
from helpers import RecordingEventLogger as EventLogger


class TtyStringIO(StringIO):
    def isatty(self):
        return True


class LoggingCommandsScriptsTests(unittest.TestCase):
    def test_event_logger_prefixes_each_multiline_terminal_line(self):
        logger = TerminalEventLogger(color=False)
        stderr = StringIO()

        with patch("sys.stderr", stderr):
            logger.error("kuhbs", "Upgrade order failed:\napp-signal: apt failed")

        lines = stderr.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.startswith("ERROR     kuhbs") for line in lines))

    def test_command_runner_kills_and_reaps_process_group_before_reraising_interrupt(self):
        logger = TerminalEventLogger(color=False, stdout=False)
        runner = CommandRunner(logger)
        real_popen = subprocess.Popen
        processes = []

        def interrupting_popen(*args, **kwargs):
            # Start a real child so the regression proves Ctrl+C leaves no process behind
            process = real_popen(*args, **kwargs)
            processes.append(process)

            def interrupt():
                raise KeyboardInterrupt

            process.communicate = interrupt
            return process

        with patch("kuhbs.command.subprocess.Popen", side_effect=interrupting_popen):
            with self.assertRaises(KeyboardInterrupt):
                runner.run(["python3", "-c", "import time; time.sleep(60)"])

        process = processes[0]
        was_reaped = process.returncode is not None
        if not was_reaped:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                # RED cleanup prevents the intentionally failing test from leaking its child
                os.killpg(process.pid, 9)
                process.wait()
        self.assertTrue(was_reaped, "interrupted command process was killed but left unreaped")

    def test_event_logger_writes_status_command_and_exit_to_terminal(self):
        logger = TerminalEventLogger(color=False)
        stdout = StringIO()

        with patch("sys.stdout", stdout):
            logger.status("signal", "Creating app-signal")
            logger.command("dom0", ["qvm-create", "app-signal"])
            logger.exit("dom0", 0, "qvm-create app-signal")

        terminal_text = stdout.getvalue()
        self.assertIn("STATUS", terminal_text)
        self.assertIn("COMMAND", terminal_text)
        self.assertIn("qvm-create app-signal", terminal_text)
        self.assertIn("EXIT", terminal_text)

    def test_event_logger_uses_configured_source_width(self):
        logger = TerminalEventLogger(color=False, source_width=10)
        stdout = StringIO()

        with patch("sys.stdout", stdout):
            logger.status("vm", "done")

        self.assertIn("STATUS    vm         done", stdout.getvalue())

    def test_event_logger_colors_terminal_stdout(self):
        logger = TerminalEventLogger()

        with patch("sys.stdout.isatty", return_value=True):
            colored = logger._stdout_line("STATUS", "kuhbs", "Creating Signal2")

        self.assertIn("\033[", colored)

    def test_event_logger_colors_output_and_exit_by_status(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            logger = EventLogger(log_path=tmp_path / "kuhbs.log")

            with patch("sys.stdout.isatty", return_value=True):
                ok_exit = logger._stdout_line("EXIT", "app-signal", "0")
                bad_exit = logger._stdout_line("EXIT", "app-signal", "2")
                command = logger._stdout_line("COMMAND", "dom0", "qvm-create app-signal")
                ok_output = logger._stdout_line("OUTPUT", "app-signal", "done", message_color="green")
                bad_output = logger._stdout_line("OUTPUT", "app-signal", "boom", message_color="red")

            self.assertIn("\033[38;5;46m", ok_exit)
            self.assertIn("\033[38;5;196m", bad_exit)
            self.assertIn("\033[38;5;15mqvm-create app-signal", command)
            self.assertIn("\033[38;5;46mdone", ok_output)
            self.assertIn("\033[38;5;196mboom", bad_output)

    def test_event_logger_maps_successful_output_to_green(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            logger = EventLogger(log_path=tmp_path / "kuhbs.log")

            with patch.object(logger, "_write") as write:
                logger.output("app-signal", "done\n", success=True)
                logger.output("app-signal", "boom\n", success=False)

            self.assertEqual(write.call_args_list[0].kwargs["message_color"], "green")
            self.assertEqual(write.call_args_list[1].kwargs["message_color"], "red")

    def test_event_logger_colors_known_vm_sources_by_configured_label(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            logger = EventLogger(log_path=tmp_path / "kuhbs.log")
            logger.set_source_label("app-signal", "green")

            with patch("sys.stdout.isatty", return_value=True):
                colored = logger._stdout_line("COMMAND", "app-signal", "qvm-prefs app-signal memory 4096")

            # VM source color follows the configured Qubes label, while the command stays white.
            self.assertIn("\033[38;5;46mapp-signal", colored)
            self.assertIn("\033[38;5;15mqvm-prefs app-signal memory 4096", colored)

    def test_event_logger_completes_interactive_command_line_with_colored_status(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            logger = EventLogger(log_path=tmp_path / "kuhbs.log")
            tty = TtyStringIO()

            with patch("sys.stdout", tty):
                logger.command("dom0", ["qvm-check", "app-signal"])
                self.assertFalse(tty.getvalue().endswith("\n"))
                logger.exit("dom0", 0)
                logger.command("dom0", ["qvm-check", "tpl-signal"])
                logger.exit("dom0", 1)

            stdout_text = tty.getvalue()
            self.assertIn("\033[38;5;46m0", stdout_text)
            self.assertIn("\033[38;5;196m1", stdout_text)
            self.assertEqual(stdout_text.count("COMMAND"), 2)
            self.assertNotIn("EXIT", stdout_text)
            self.assertIn("EXIT      dom0                      0", (tmp_path / "kuhbs.log").read_text())
            self.assertIn("EXIT      dom0                      1", (tmp_path / "kuhbs.log").read_text())

    def test_event_logger_does_not_attach_exit_to_another_thread_command(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            logger = EventLogger(log_path=tmp_path / "kuhbs.log", color=False)
            tty = TtyStringIO()
            first_command_written = Event()
            release_first_exit = Event()

            def first_worker():
                logger.command("vm-a", ["qvm-check", "vm-a"])
                first_command_written.set()
                release_first_exit.wait()
                logger.exit("vm-a", 0)

            def second_worker():
                first_command_written.wait()
                logger.command("vm-b", ["qvm-check", "vm-b"])
                logger.exit("vm-b", 1)
                release_first_exit.set()

            with patch("sys.stdout", tty):
                threads = [Thread(target=first_worker), Thread(target=second_worker)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            stdout_lines = [line for line in tty.getvalue().splitlines() if line]
            self.assertEqual(len(stdout_lines), 2)
            self.assertTrue(any(line.startswith("COMMAND   vm-a") and line.endswith(" 0") for line in stdout_lines))
            self.assertTrue(any(line.startswith("COMMAND   vm-b") and line.endswith(" 1") for line in stdout_lines))
            self.assertFalse(any(line.startswith("EXIT") for line in stdout_lines))

    def test_event_logger_newlines_exit_when_another_thread_has_pending_command(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            logger = EventLogger(log_path=tmp_path / "kuhbs.log", color=False)
            tty = TtyStringIO()
            first_command_written = Event()
            second_command_written = Event()

            def first_worker():
                logger.command("vm-a", ["qvm-check", "vm-a"])
                first_command_written.set()
                second_command_written.wait()
                logger.exit("vm-a", 0)

            def second_worker():
                first_command_written.wait()
                logger.command("vm-b", ["qvm-check", "vm-b"])
                second_command_written.set()

            with patch("sys.stdout", tty):
                threads = [Thread(target=first_worker), Thread(target=second_worker)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            stdout_lines = [line for line in tty.getvalue().splitlines() if line]
            self.assertEqual(len(stdout_lines), 1)
            self.assertTrue(stdout_lines[0].startswith("COMMAND   vm-a"))
            self.assertTrue(stdout_lines[0].endswith(" 0"))
            self.assertNotIn("vm-b", tty.getvalue())
            self.assertFalse(any(line.startswith("EXIT") for line in stdout_lines))

    def test_event_logger_prints_parallel_worker_commands_atomically_on_exit(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            logger = EventLogger(log_path=tmp_path / "kuhbs.log", color=False)
            tty = TtyStringIO()
            first_command_written = Event()
            release_first_exit = Event()

            def first_worker():
                logger.command("sta-code-kuhbs", ["qvm-check", "--quiet", "sta-code-kuhbs"])
                first_command_written.set()
                release_first_exit.wait()
                logger.exit("sta-code-kuhbs", 0)

            def second_worker():
                first_command_written.wait()
                logger.command("sta-bot-fish-pr", ["qvm-check", "--quiet", "sta-bot-fish-pr"])
                logger.exit("sta-bot-fish-pr", 1)
                release_first_exit.set()

            with patch("sys.stdout", tty):
                threads = [Thread(target=first_worker), Thread(target=second_worker)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            stdout_lines = [line for line in tty.getvalue().splitlines() if line]
            self.assertEqual(len(stdout_lines), 2)
            self.assertIn("COMMAND   sta-code-kuhbs            qvm-check --quiet sta-code-kuhbs 0", stdout_lines)
            self.assertIn("COMMAND   sta-bot-fish-pr           qvm-check --quiet sta-bot-fish-pr 1", stdout_lines)
            self.assertFalse(any(line.startswith("EXIT") for line in stdout_lines))

    def test_command_runner_completes_command_before_captured_output(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            logger = EventLogger(log_path=tmp_path / "kuhbs.log", color=False)
            runner = CommandRunner(logger=logger)
            tty = TtyStringIO()

            with patch("sys.stdout", tty):
                runner.run(["python3", "-c", "print('ok')"])

            self.assertRegex(tty.getvalue(), r"COMMAND.*python3.* 0\nOUTPUT.*ok")

    def test_command_runner_run_for_kuh_sets_source_without_parsing_command_args(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            logger = EventLogger(log_path=tmp_path / "kuhbs.log", color=False, stdout=False)
            runner = CommandRunner(logger=logger)

            runner.run_for_kuh("tpl-not-from-argv", ["python3", "-c", "pass"])

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("COMMAND   tpl-not-from-argv", log_text)
            self.assertNotIn("COMMAND   app-signal", log_text)

    def test_command_runner_colors_captured_output_by_exit_status(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            logger = EventLogger(log_path=tmp_path / "kuhbs.log", color=False, stdout=False)
            runner = CommandRunner(logger=logger)

            with patch.object(logger, "output") as output:
                runner.run(["python3", "-c", "print('ok')"])
                runner.run(["python3", "-c", "print('bad'); raise SystemExit(2)"], check=False)

            self.assertEqual(output.call_args_list[0].kwargs["success"], True)
            self.assertEqual(output.call_args_list[1].kwargs["success"], False)

    def test_command_runner_visible_output_skips_output_log_lines(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            logger = EventLogger(log_path=tmp_path / "kuhbs.log", color=False, stdout=False)
            runner = CommandRunner(logger=logger)

            result = runner.run(["python3", "-c", "print('hook visible output')"], visible_output=True)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertEqual(result.returncode, 0)
            self.assertIn("COMMAND   dom0", log_text)
            self.assertIn("EXIT      dom0", log_text)
            self.assertNotIn("OUTPUT", log_text)

    def test_command_runner_visible_output_quiet_hook_keeps_status_on_command_line(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            logger = EventLogger(log_path=tmp_path / "kuhbs.log", color=False)
            runner = CommandRunner(logger=logger)
            tty = TtyStringIO()

            with patch("sys.stdout", tty):
                runner.run(["python3", "-c", "pass"], visible_output=True)

            self.assertRegex(tty.getvalue(), r"COMMAND.*python3.* 0\n$")
            self.assertNotIn("EXIT", tty.getvalue())

    def test_command_runner_visible_output_noisy_hook_keeps_output_live_then_exit_line(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            logger = EventLogger(log_path=tmp_path / "kuhbs.log", color=False)
            runner = CommandRunner(logger=logger)
            tty = TtyStringIO()

            with patch("sys.stdout", tty):
                runner.run(["python3", "-c", "print('hook visible output')"], visible_output=True)

            stdout_text = tty.getvalue()
            self.assertRegex(stdout_text, r"COMMAND.*python3.*\nhook visible output\nEXIT.*0\n$")

    def test_concatenate_script_fragments_fails_on_shebang_and_orders_directory_files_by_name(self):
        with tempfile.TemporaryDirectory() as td:
            scripts = Path(td) / "scripts"
            scripts.mkdir()
            (scripts / "two.sh").write_text("echo two\n")
            (scripts / "one.sh").write_text("echo one\n")

            concatenated = concatenate_script_fragments([scripts])

            self.assertTrue(concatenated.startswith("#!/bin/bash\nset -e -x\n"))
            self.assertLess(concatenated.index("echo one"), concatenated.index("echo two"))

            (scripts / "bad.sh").write_text("#!/bin/bash\necho bad\n")
            with self.assertRaisesRegex(ValueError, "shebang"):
                concatenate_script_fragments([scripts])

    def test_concatenate_script_fragments_keeps_explicit_list_order(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            shared = tmp_path / "shared"
            local = tmp_path / "local"
            shared.mkdir()
            local.mkdir()
            http = shared / "http.sh"
            update = shared / "update.sh"
            basics = shared / "basics.sh"
            usb = local / "usb.sh"
            tool = local / "tool.sh"
            http.write_text("echo http\n")
            update.write_text("echo update\n")
            basics.write_text("echo basics\n")
            usb.write_text("echo usb\n")
            tool.write_text("echo tool\n")

            concatenated = concatenate_script_fragments([http, update, basics, usb, tool])

            self.assertLess(concatenated.index("echo http"), concatenated.index("echo update"))
            self.assertLess(concatenated.index("echo update"), concatenated.index("echo basics"))
            self.assertLess(concatenated.index("echo basics"), concatenated.index("echo usb"))
            self.assertLess(concatenated.index("echo usb"), concatenated.index("echo tool"))


if __name__ == "__main__":
    unittest.main()
