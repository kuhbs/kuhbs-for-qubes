# Purpose: Command execution wrapper for dom0 and qvm calls
# Scope: Centralizes logging, process cleanup, and checked failures
from __future__ import annotations

from dataclasses import dataclass
import subprocess
import os
import signal
from shlex import join
import sys
import threading

from .log import EventLogger


@dataclass(frozen=True)
class CommandResult:
    # Keep the captured exit status and streams returned by checked commands
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner:
    # All subprocess entrypoints go through this class so audit logging and process cleanup stay identical
    def __init__(self, logger: EventLogger):
        # Store the logger on the object for the command helpers below
        self.logger = logger
        # Ctrl-C in a batch must be able to kill qvm-run/tar children owned by worker threads
        self._active_processes: set[subprocess.Popen[str]] = set()
        self._active_processes_lock = threading.Lock()

    def _register_process(self, process: subprocess.Popen[str]) -> subprocess.Popen[str]:
        # Workers share one runner, so active children are guarded by a small lock
        with self._active_processes_lock:
            self._active_processes.add(process)
        return process

    def _unregister_process(self, process: subprocess.Popen[str]) -> None:
        # Forget a tracked child process once finish or cleanup has consumed it
        with self._active_processes_lock:
            self._active_processes.discard(process)

    def kill_active_processes(self) -> None:
        # User pressed Ctrl-C; kill everything KUHBS started instead of waiting on stuck qvm-run jobs
        with self._active_processes_lock:
            processes = list(self._active_processes)
        for process in processes:
            if process.poll() is None:
                try:
                    # Commands are started in their own process group so shell pipelines die together
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    # Another cleanup path already reaped this process group
                    continue
                except OSError:
                    # Fall back to the direct child when the OS refuses group delivery
                    process.kill()

    def run(
        self,
        args: list[str],
        *,
        source: str = "dom0",
        check: bool = True,
        visible_output: bool = False,
        log: bool = True,
    ) -> CommandResult:
        # Normal operation captures output for colored OUTPUT lines; hooks can stream live to the terminal
        # Run the requested operation while preserving KUHBS logging and error handling
        if log:
            self.logger.command(source, args)
        if visible_output:
            # Hooks are user-authored scripts; stream output, but keep quiet hooks as one command + exit-code line
            # Each command owns a process group so Ctrl-C cleanup reaches qvm-run/tar children
            completed = self._register_process(subprocess.Popen(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0, start_new_session=True))
            try:
                if completed.stdout is not None:
                    while True:
                        char = completed.stdout.read(1)
                        if char == "":
                            break
                        if log:
                            self.logger._finish_pending_command_stdout()
                        sys.stdout.write(char)
                        sys.stdout.flush()
                completed.wait()
            except KeyboardInterrupt:
                # Kill and reap before unregistering so interrupted children cannot remain as zombies
                self.kill_active_processes()
                completed.wait()
                raise
            finally:
                if completed.stdout is not None:
                    completed.stdout.close()
                self._unregister_process(completed)
            stdout = ""
            stderr = ""
        else:
            # Captured commands can still be shell pipelines, so they get a killable process group too
            completed = self._register_process(subprocess.Popen(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True))
            try:
                stdout, stderr = completed.communicate()
            except KeyboardInterrupt:
                # Captured commands need the same kill-and-reap guarantee before active tracking is cleared
                self.kill_active_processes()
                completed.wait()
                raise
            finally:
                self._unregister_process(completed)
            success = completed.returncode == 0
        if log:
            self.logger.exit(source, completed.returncode, join(args))
        if not visible_output and log:
            if stdout:
                self.logger.output(source, stdout, success=success)
            if stderr:
                self.logger.output(source, stderr, success=success)
        if check and completed.returncode != 0:
            raise RuntimeError(f"command failed with exit {completed.returncode}: {join(args)}")
        return CommandResult(returncode=completed.returncode, stdout=stdout, stderr=stderr)

    def run_for_dom0(
        self,
        args: list[str],
        *,
        check: bool = True,
        visible_output: bool = False,
        log: bool = True,
    ) -> CommandResult:
        # All commands execute in dom0; this keeps the log source explicit for dom0-scoped work
        # Run a dom0 command with the normal dom0 log source
        return self.run(args, source="dom0", check=check, visible_output=visible_output, log=log)

    def run_for_kuh(
        self,
        kuh_name: str,
        args: list[str],
        *,
        check: bool = True,
        visible_output: bool = False,
        log: bool = True,
    ) -> CommandResult:
        # All commands still execute in dom0; kuh_name only explains who the command concerns in logs
        # Run a command whose log source is the target kuh name
        return self.run(args, source=kuh_name, check=check, visible_output=visible_output, log=log)

    def start_for_kuh(self, kuh_name: str, args: list[str], *, detached: bool = False) -> subprocess.Popen[str]:
        # Async terminal launches also carry an explicit concerned kuh in the log source column
        return self.start(args, source=kuh_name, detached=detached)

    def shell_for_dom0(self, command: str, *, check: bool = True, visible_output: bool = False) -> CommandResult:
        # Dom0 shell pipelines are visually scoped to dom0 even when they involve qvm/qrexec streams
        return self.shell(command, source="dom0", check=check, visible_output=visible_output)

    def start(self, args: list[str], *, source: str = "dom0", detached: bool = False) -> subprocess.Popen[str]:
        # Start a background process that later polling/finish calls can observe
        self.logger.command(source, args)
        if detached:
            # Fire-and-forget terminals have no owner left to drain pipes or collect exit status
            return self._register_process(subprocess.Popen(args, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True))
        # Long-running qvm-run terminal launches need polling while their terminal stays visible
        # Non-detached background commands are still owned by this runner until finish() consumes them
        return self._register_process(subprocess.Popen(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True))

    def poll(self, process: subprocess.Popen[str]) -> int | None:
        # Return the process exit status when it is ready without blocking
        return process.poll()

    def finish(self, process: subprocess.Popen[str], args: list[str], *, source: str = "dom0", check: bool = True, log: bool = True) -> CommandResult:
        # Async terminal runs may be UI wrappers whose exit is not the script result
        try:
            stdout, stderr = process.communicate()
        finally:
            self._unregister_process(process)
        success = process.returncode == 0
        if log:
            self.logger.exit(source, process.returncode, join(args))
        if stdout:
            self.logger.output(source, stdout, success=success)
        if stderr:
            self.logger.output(source, stderr, success=success)
        if check and process.returncode != 0:
            raise RuntimeError(f"command failed with exit {process.returncode}: {join(args)}")
        return CommandResult(returncode=process.returncode, stdout=stdout or "", stderr=stderr or "")

    def shell(self, command: str, *, source: str = "dom0", check: bool = True, visible_output: bool = False) -> CommandResult:
        # Shell mode is reserved for intentional pipelines such as configured backup/restore streaming
        return self.run(["bash", "-lc", command], source=source, check=check, visible_output=visible_output)
