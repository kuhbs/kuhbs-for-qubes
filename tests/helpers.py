# Purpose: Shared test defaults for current KUHBS config shape
# Scope: Tests keep tiny overrides while still passing real required defaults explicitly
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from shlex import join

from kuhbs.command import CommandResult, CommandRunner
from kuhbs.config import load_defaults
from kuhbs.log import EventLogger
from kuhbs.operations import OperationContext


ROOT = Path(__file__).resolve().parents[1]
REAL_DEFAULTS = load_defaults(ROOT / "defaults.yml")


def deep_merge(defaults: dict, override: dict) -> dict:
    # Tests override only the paths/probes they isolate; required defaults still come from defaults.yml
    merged = deepcopy(defaults)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def current_defaults(overrides: dict) -> dict:
    return deep_merge(REAL_DEFAULTS, overrides)


class RecordingEventLogger(EventLogger):
    # Tests keep command/status evidence without restoring a production log sink
    def __init__(self, log_path: str | Path, *, color: bool = True, stdout: bool = True, source_width: int = 25):
        super().__init__(color=color, stdout=stdout, source_width=source_width)
        self.record_path = Path(log_path)

    def _record(self, event: str, source: str, message: str) -> None:
        # Plain temporary records let operation tests assert commands without terminal capture plumbing
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        with self.record_path.open("a", encoding="utf-8") as handle:
            for line in message.splitlines() or [""]:
                handle.write(f"{self._line(event, source, line)}\n")

    def _write(self, event: str, source: str, message: str, *, message_color: str | None = None) -> None:
        # Parallel test workers keep terminal output and recorded evidence in the same event order
        with self._lock:
            self._record(event, source, message)
            super()._write(event, source, message, message_color=message_color)

    def command(self, source: str, command: list[str] | str, *, compact: bool = True) -> None:
        message = command if isinstance(command, str) else join(command)
        # Keep test records in the same critical section as their terminal command line
        with self._lock:
            self._record("COMMAND", source, message)
            super().command(source, command, compact=compact)

    def exit(self, source: str, code: int, summary: str = "") -> None:
        # Exit records stay paired with the corresponding terminal exit line under parallel workers
        with self._lock:
            self._record("EXIT", source, str(code))
            super().exit(source, code, summary)


class MappingRunner(CommandRunner):
    # Small tests can map exact command tuples to return codes instead of defining one runner class each
    def __init__(self, logger, *, returncodes=None, stdout=None):
        super().__init__(logger)
        self.returncodes = {tuple(key): value for key, value in (returncodes or {}).items()}
        self.stdout = {tuple(key): value for key, value in (stdout or {}).items()}
        self.commands: list[list[str]] = []

    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        self.commands.append(list(args))
        if log:
            self.logger.command(source, args)
        key = tuple(args)
        if key in self.returncodes:
            returncode = self.returncodes[key]
        elif args[:3] == ["qvm-check", "--quiet", "--paused"]:
            # Most fake VMs are not paused; tests opt into that less common state explicitly
            returncode = 1
        elif len(args) >= 3 and args[-3:-1] == ["test", "-f"] and args[-1].endswith((".rules", ".yml")):
            # Generic operation fixtures have no Snitch policy file unless a dedicated test provides one
            returncode = 1
        else:
            returncode = 0
        output = self.stdout.get(key, "")
        if returncode == 0 and args and args[0] in {"qvm-create", "qvm-clone"}:
            # Successful fake creation makes later existence checks reflect the same VM lifecycle
            self.returncodes[("qvm-check", "--quiet", args[-1])] = 0
        elif returncode == 0 and args and args[0] == "qvm-remove":
            # Successful fake removal keeps later idempotency probes consistent with the command history
            self.returncodes[("qvm-check", "--quiet", args[-1])] = 1
        if log:
            self.logger.exit(source, returncode, " ".join(args))
        if check and returncode != 0:
            raise RuntimeError("unexpected checked command")
        return CommandResult(returncode=returncode, stdout=output)

    def shell(self, command, *, source="dom0", check=True, visible_output=False):
        return self.run(["bash", "-lc", command], source=source, check=check, visible_output=visible_output)

    def start(self, args, *, source="dom0", detached=False):
        # Unit tests record background launches without creating real terminals or Qubes processes
        self.commands.append(list(args))
        self.logger.command(source, args)
        return object()

    def poll(self, process):
        # Explicit fake processes finish immediately without production runner sentinel behavior
        return 0

    def finish(self, process, args, *, source="dom0", check=True, log=True):
        # Fake background completion is owned here so CommandRunner handles real Popen objects only
        return CommandResult(returncode=0)


class RunnerContext(OperationContext):
    # Return one injected runner instance so tests can assert complete command history
    def __init__(self, defaults, logger, runner):
        super().__init__(defaults, logger)
        self._runner = runner

    @property
    def runner(self):
        return self._runner
