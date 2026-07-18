# Purpose: Generate and remove dom0 desktop launchers for KUHBS targets
# Scope: Launcher files are derived artifacts; YAML remains the source of truth
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex
import shutil

from .config import resolve_path
from .model import resolve_kuhs


@dataclass(frozen=True)
class LauncherSpec:
    # Normalized launcher data used only to generate desktop entries
    # The user-invoked i3/rofi launcher named "Autostart" reads launcher YAML independently
    launcher_id: str
    name: str
    target: str
    user: str
    dispvm: bool
    command: str
    run_in_terminal: bool
    shutdown_on_exit_0: bool
    icon: str


def desktop_root(ctx) -> Path:
    # Keep desktop-entry output rooted in defaults so dom0 and tests can redirect it
    return resolve_path(ctx.defaults["paths"]["desktop_applications"])


def _write(path: Path, content: str, mode: int = 0o644) -> Path:
    # Desktop entries are regenerated wholesale, so simple overwrite is safest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def _qvm_run_command(spec: LauncherSpec) -> str:
    # Build the guest-side command string before quoting the full qvm-run argv
    command = spec.command
    if spec.shutdown_on_exit_0:
        command = f"{command} && /usr/bin/sudo --non-interactive /sbin/shutdown -h now"
    if spec.run_in_terminal:
        # Terminal launchers need shell semantics for snippets, pipes, and the failure hold
        inner = shlex.join(["bash", "-lc", f"set -x; {command} || cat"])
        title = f"{spec.target}: {spec.name}"
        return f"/usr/bin/xfce4-terminal --title {shlex.quote(title)} --command {shlex.quote(inner)}"
    return command


def _desktop_quote(arg: str) -> str:
    # Desktop Entry Exec values are not POSIX shell, so use its double-quote escaping rules
    reserved = '"\'\\><~|&;$*?#()`%'
    if not arg or any(char.isspace() or char in reserved for char in arg):
        # Percent remains a field-code marker inside quotes, so double it before escaping quoted characters
        escaped = arg.replace('%', '%%').replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$')
        return f'"{escaped}"'
    return arg


def _desktop_exec_args(args: list[str]) -> str:
    # Keep argv boundaries explicit for desktop-file-validate and menu implementations
    return " ".join(_desktop_quote(arg) for arg in args)


def _desktop_value(value: str) -> str:
    # Key-file parsing consumes one backslash layer before the Exec parser sees its quoting.
    return value.replace("\\", "\\\\")


def _desktop_exec(spec: LauncherSpec) -> str:
    # Terminal launcher entries go back through the CLI so fallback probing stays in one place
    if spec.command == "terminal":
        args = ["/usr/bin/kuhbs", "terminal", spec.target, spec.user]
        if spec.dispvm:
            args.append("--dispvm")
        if spec.shutdown_on_exit_0:
            args.append("--shutdown-on-exit-0")
        return _desktop_exec_args(args)
    # Desktop files are parsed by desktop-entry readers, not by a POSIX shell
    args = ["/usr/bin/qvm-run", "--user", spec.user]
    if spec.dispvm:
        args.append("--dispvm")
    args.extend([spec.target, _qvm_run_command(spec)])
    return _desktop_exec_args(args)


def _desktop_entry(spec: LauncherSpec) -> str:
    # Use a stable KUHBS prefix so generated launchers are easy to spot in menus
    name = f"KUHBS {spec.name} ({spec.user}@{spec.target})"
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        f"Name={name}",
        f"Exec={_desktop_value(_desktop_exec(spec))}",
        f"Icon={spec.icon}",
        "Terminal=false",
        "Categories=System;X-KUHBS;",
        "",
    ])


def launcher_specs(defaults: dict, definition: dict) -> list[LauncherSpec]:
    # Resolve all configured launcher rows into immutable desktop-entry specs
    specs: list[LauncherSpec] = []
    # Launcher icons are validated as launcher-icons/<id>.svg beside the installed KUHB definition
    kuhb_root = resolve_path(defaults["paths"]["kuhbs"]) / definition["id"]
    for kuh in resolve_kuhs(definition):
        # Launchers are explicit YAML entries; there is no implicit terminal row
        for launcher in kuh.config["launchers"]:
            icon = str((kuhb_root / "launcher-icons" / f"{launcher['id']}.svg").resolve())
            specs.append(LauncherSpec(
                launcher_id=launcher["id"],
                name=launcher["name"],
                target=kuh.name,
                user=launcher["user"],
                dispvm=launcher["dispvm"] is True,
                command=launcher["command"],
                run_in_terminal=launcher["run_in_terminal"] is True,
                shutdown_on_exit_0=launcher["shutdown_on_exit_0"] is True,
                icon=icon,
            ))
    return specs


def _desktop_path(root: Path, spec: LauncherSpec) -> Path:
    # Validated components exclude +, so it preserves exact ownership for stale-launcher globs
    return root / f"kuhbs-{spec.target}+{spec.user}+{spec.launcher_id}.desktop"


def generate_launchers(ctx, definition: dict) -> None:
    # Creation refreshes every derived launcher after VM config has been resolved
    kuhb_id = definition["id"]
    desktop_dir = desktop_root(ctx)
    for spec in launcher_specs(ctx.defaults, definition):
        _write(_desktop_path(desktop_dir, spec), _desktop_entry(spec))
    ctx.logger.status(kuhb_id, f"Generated desktop entries in {desktop_dir}")


def remove_launchers(ctx, definition: dict) -> None:
    # Remove current and stale desktop entries for every resolved kuh target
    desktop_dir = desktop_root(ctx)
    kuhb_id = definition["id"]
    paths = {_desktop_path(desktop_dir, spec) for spec in launcher_specs(ctx.defaults, definition)}
    for kuh in resolve_kuhs(definition):
        # Launcher names are generated from resolved kuh names, so remove stale entries for each kuh
        paths.update(desktop_dir.glob(f"kuhbs-{kuh.name}+*.desktop"))
    for path in paths:
        path.unlink(missing_ok=True)
    if desktop_dir.exists() and not any(desktop_dir.iterdir()):
        try:
            # Same-order Remove workers may both observe the shared directory empty; the first deletion wins
            shutil.rmtree(desktop_dir)
        except FileNotFoundError:
            pass
    ctx.logger.status(kuhb_id, "Removed desktop entries")
