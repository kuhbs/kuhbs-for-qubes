# Purpose: Human-readable command and status output
# Scope: Keep terminal feedback colorful and safe across parallel workers
from __future__ import annotations

import os
from shlex import join
import sys
import threading


ANSI = {
    # Palette is tuned for dom0 terminals: high contrast plus KUHBS purple accents
    "reset": "\033[0m",
    "bold": "\033[1m",
    "red": "\033[38;5;196m",
    "orange": "\033[38;5;202m",
    "yellow": "\033[38;5;226m",
    "green": "\033[38;5;46m",
    "gray": "\033[38;5;249m",
    "white": "\033[38;5;15m",
    "blue": "\033[38;5;39m",
    "purple": "\033[38;5;129m",
}

EVENT_COLORS = {
    "DEBUG": "blue",
    "INFO": "green",
    "STATUS": "purple",
    "WARNING": "orange",
    "ERROR": "red",
    "COMMAND": "blue",
    "OUTPUT": "gray",
}


class EventLogger:
    # One logger serializes human CLI output without creating persistent records
    def __init__(self, *, color: bool = True, stdout: bool = True, source_width: int = 25):
        # Store constructor inputs on the object for the small helper methods below
        self.color = color
        self.stdout = stdout
        self.source_width = source_width
        # Parallel upgrade workers share one logger, so stdout state needs one lock
        self._lock = threading.RLock()
        self._main_thread_id = threading.get_ident()
        # Interactive foreground commands keep their line open; worker commands print atomically on exit
        self._pending_command: tuple[int, str, str] | None = None
        self._worker_pending_commands: dict[int, tuple[str, str]] = {}
        # Resolved kuh labels color the source column
        self._source_labels: dict[str, str] = {}

    def _color_enabled(self) -> bool:
        # Use ANSI colors only for interactive terminal output
        return self.color and self.stdout and sys.stdout.isatty() and "NO_COLOR" not in os.environ

    def _compact_commands_enabled(self) -> bool:
        # Only mutate terminal lines on a real tty; pipes need complete newline-delimited output
        return self.stdout and sys.stdout.isatty()

    def _paint(self, text: str, color: str, *, bold: bool = False) -> str:
        # Wrap text in one ANSI color and reset sequence when coloring is enabled
        if not self._color_enabled():
            return text
        prefix = ANSI["bold"] if bold else ""
        return f"{prefix}{ANSI.get(color, '')}{text}{ANSI['reset']}"

    def set_source_label(self, source: str, label: str) -> None:
        # Labels are visual hints for the source column, not command-routing metadata
        self._source_labels[source] = label

    def source_name(self, source: str) -> str:
        # Prompt lists use the same Qubes label colors as the source column
        return self._paint(source, self._source_color(source), bold=True)

    def configure_colors(self, colors: dict) -> None:
        # defaults.yml owns palette IDs; nested badge styles are ignored by the simple ANSI map.
        for name, value in colors.items():
            if isinstance(value, int):
                ANSI[name] = f"\033[38;5;{value}m"

    def _source_color(self, source: str) -> str:
        # Map KUHBS sources and Qubes labels to configured ANSI colors
        if source == "kuhbs":
            return "purple"
        if source == "dom0":
            return "gray"
        return self._source_labels.get(source, "")

    def _line(self, event: str, source: str, message: str) -> str:
        # Align plain terminal fields before adding optional color
        return f"{event:<7}   {source:<{self.source_width}} {message}"

    def _stdout_line(self, event: str, source: str, message: str, *, message_color: str | None = None) -> str:
        # Color terminal fields while non-TTY output stays plain
        if not self._color_enabled():
            return self._line(event, source, message)
        event_color = EVENT_COLORS.get(event, "gray")
        if event == "EXIT":
            event_color = "green" if message == "0" else "red"
            message_color = event_color
        if event == "COMMAND":
            # Keep command text bright enough to read during long Qubes runs
            message_color = "white"
        painted_event = self._paint(f"{event:<7}", event_color, bold=True)
        painted_source = self._paint(f"{source:<{self.source_width}}", self._source_color(source))
        painted_message = self._paint(message, message_color or (event_color if event in {"ERROR", "WARNING"} else "gray"))
        return f"{painted_event}   {painted_source} {painted_message}"

    def _stdout_command_prefix(self, source: str, message: str) -> str:
        # Build the compact command prefix shown before its exit code
        return self._stdout_line("COMMAND", source, message)

    def _stdout_return_code(self, code: int) -> str:
        # Use green for success and red for command failure
        color = "green" if code == 0 else "red"
        return self._paint(str(code), color, bold=True)

    def _finish_pending_command_stdout(self, code: int | None = None, *, thread_id: int | None = None) -> None:
        # Complete only the pending compact command owned by the selected thread
        with self._lock:
            if self._pending_command is None:
                return
            if thread_id is not None and self._pending_command[0] != thread_id:
                return
            if code is None:
                print(file=sys.stdout, flush=True)
            else:
                sys.stdout.write(f" {self._stdout_return_code(code)}\n")
                sys.stdout.flush()
            self._pending_command = None

    def _completed_command_stdout_line(self, source: str, message: str, code: int) -> str:
        # Render a finished command on one line when output arrived before its exit
        return f"{self._stdout_line('COMMAND', source, message)} {self._stdout_return_code(code)}"

    def _write(self, event: str, source: str, message: str, *, message_color: str | None = None) -> None:
        # Prefix every line so multiline errors remain readable in terminal history
        with self._lock:
            if self.stdout:
                self._finish_pending_command_stdout()
                stream = sys.stderr if event in {"ERROR", "WARNING"} else sys.stdout
                for line in message.splitlines() or [""]:
                    print(self._stdout_line(event, source, line, message_color=message_color), file=stream)


    def info(self, source: str, message: str) -> None:
        # Emit an INFO event through the shared writer
        self._write("INFO", source, message)

    def warning(self, source: str, message: str) -> None:
        # Emit a WARNING event through the shared writer
        self._write("WARNING", source, message)

    def error(self, source: str, message: str) -> None:
        # Emit an ERROR event through the shared writer
        self._write("ERROR", source, message)

    def status(self, source: str, message: str) -> None:
        # Emit a STATUS event through the shared writer
        self._write("STATUS", source, message)

    def command(self, source: str, command: list[str] | str, *, compact: bool = True) -> None:
        # Start a command event and defer its newline only for compact TTY output
        message = command if isinstance(command, str) else join(command)
        with self._lock:
            if self.stdout:
                self._finish_pending_command_stdout()
                thread_id = threading.get_ident()
                if compact and self._compact_commands_enabled() and thread_id != self._main_thread_id:
                    # Parallel workers print the completed command+exit atomically so other workers cannot interleave
                    self._worker_pending_commands[thread_id] = (source, message)
                elif compact and self._compact_commands_enabled():
                    # Foreground commands stay live so visible output can still break onto the next line
                    sys.stdout.write(self._stdout_command_prefix(source, message))
                    sys.stdout.flush()
                    self._pending_command = (thread_id, source, message)
                else:
                    print(self._stdout_line("COMMAND", source, message), file=sys.stdout)

    def output(self, source: str, message: str, *, success: bool | None = None) -> None:
        # Color captured output by the completed process status instead of guessing from text
        message_color = None
        if success is not None:
            message_color = "green" if success else "red"
        for line in message.splitlines():
            self._write("OUTPUT", source, line, message_color=message_color)

    def exit(self, source: str, code: int, summary: str = "") -> None:
        # Pair an exit code with the calling thread's pending compact command
        with self._lock:
            thread_id = threading.get_ident()
            pending_matches = self._pending_command is not None and self._pending_command[:2] == (thread_id, source)
            worker_pending = self._worker_pending_commands.pop(thread_id, None)
            if self.stdout and pending_matches:
                self._finish_pending_command_stdout(code, thread_id=thread_id)
            elif self.stdout and worker_pending is not None:
                pending_source, pending_message = worker_pending
                print(self._completed_command_stdout_line(pending_source, pending_message, code), file=sys.stdout)
            elif self.stdout:
                if self._pending_command is not None:
                    # Another foreground line owns stdout, so end it before printing this standalone EXIT
                    self._finish_pending_command_stdout()
                print(self._stdout_line("EXIT", source, str(code)), file=sys.stdout)
