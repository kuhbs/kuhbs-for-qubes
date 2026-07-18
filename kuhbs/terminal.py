# Purpose: Terminal command resolution for visible kuh-side runs
# Scope: Wrapper commands keep successful setup runs from hanging forever
from __future__ import annotations

from shlex import quote

from .qubes import run_in_kuh


def _with_title(prefix: list[str], title: str | None) -> list[str]:
    # i3 routing matches terminal titles; only KUHBS-owned launches get these labels.
    if title is None:
        return prefix
    terminal_path = prefix[0]
    if terminal_path.endswith("/xfce4-terminal") or terminal_path == "xfce4-terminal":
        return [*prefix, "--title", title]
    if terminal_path.endswith("/xterm") or terminal_path == "xterm":
        return [*prefix, "-T", title]
    raise RuntimeError(f"unsupported terminal title syntax for {terminal_path}")


def _with_process_lifetime(prefix: list[str], lifetime_sensitive: bool) -> list[str]:
    # Xfce server handoff returns before its window closes, so tracked callers need their own process
    if not lifetime_sensitive:
        return prefix
    terminal_path = prefix[0]
    if terminal_path.endswith("/xfce4-terminal") or terminal_path == "xfce4-terminal":
        return [terminal_path, "--disable-server", *prefix[1:]]
    return prefix


def terminal_command_prefix(
    ctx,
    kuh_name: str,
    *,
    title: str | None = None,
    probe: bool = True,
    lifetime_sensitive: bool = False,
) -> list[str]:
    # Persistent terminals probe because minimal templates may lack xfce4-terminal.
    terminal = ctx.defaults["terminal"]
    preferred_path = terminal["path"]
    preferred_args = list(terminal["args"])
    # A DispVM is expected to have the configured preferred terminal installed.
    # Skip probing because that check would start its persistent template AppVM.
    if not probe:
        return _with_process_lifetime(_with_title([preferred_path, *preferred_args], title), lifetime_sensitive)
    result = run_in_kuh(ctx.runner, kuh_name, ["test", "-x", preferred_path], check=False)
    if result.returncode == 0:
        return _with_process_lifetime(_with_title([preferred_path, *preferred_args], title), lifetime_sensitive)
    fallback = terminal["fallback"]
    fallback_path = fallback["path"]
    fallback_args = list(fallback["args"])
    result = run_in_kuh(ctx.runner, kuh_name, ["test", "-x", fallback_path], check=False)
    if result.returncode == 0:
        ctx.logger.warning(kuh_name, f"{preferred_path} not found, falling back to {fallback_path}")
        return _with_process_lifetime(_with_title([fallback_path, *fallback_args], title), lifetime_sensitive)
    raise RuntimeError(f"neither {preferred_path} nor {fallback_path} exists in {kuh_name}")


def dom0_terminal_command_prefix(ctx, *, title: str | None = None, lifetime_sensitive: bool = False) -> list[str]:
    # Qubes dom0 ships xfce4-terminal by default; avoid noisy local probing.
    terminal = ctx.defaults["terminal"]
    preferred_path = terminal["path"]
    preferred_args = list(terminal["args"])
    return _with_process_lifetime(_with_title([preferred_path, *preferred_args], title), lifetime_sensitive)



def _terminal_script_args(
    prefix: list[str],
    wrapper: str,
    exit_status_file: str,
    script: str,
    *,
    script_args: list[str] | None,
) -> list[str]:
    # Both VM and dom0 terminals use the same wrapper protocol so exit handling cannot drift by location
    terminal_path = prefix[0]
    forwarded_args = list(script_args or [])
    if terminal_path.endswith("/xfce4-terminal") or terminal_path == "xfce4-terminal":
        # xfce4-terminal takes one command string, so quote values that may contain spaces
        command = " ".join(quote(arg) for arg in [wrapper, exit_status_file, script, *forwarded_args])
        # The wrapper handles success/failure holding; --hold would freeze every successful run
        return [*prefix, "--command", command]
    if terminal_path.endswith("/xterm") or terminal_path == "xterm":
        return [*prefix, "-e", wrapper, exit_status_file, script, *forwarded_args]
    raise RuntimeError(f"unsupported terminal command syntax for {terminal_path}")


def terminal_script_args(ctx, kuh_name: str, wrapper: str, exit_status_file: str, script: str, *, script_args: list[str] | None = None, title: str | None = None) -> list[str]:
    # VM setup and hook scripts need a terminal whose configured binary is first checked inside that VM
    prefix = terminal_command_prefix(ctx, kuh_name, title=title, lifetime_sensitive=True)
    return _terminal_script_args(prefix, wrapper, exit_status_file, script, script_args=script_args)


def dom0_terminal_script_args(ctx, wrapper: str, exit_status_file: str, script: str, *, script_args: list[str] | None = None, title: str | None = None) -> list[str]:
    # Arbitrary dom0 hooks may ask questions, draw menus or read passwords, so each parallel worker needs its own PTY
    prefix = dom0_terminal_command_prefix(ctx, title=title, lifetime_sensitive=True)
    return _terminal_script_args(prefix, wrapper, exit_status_file, script, script_args=script_args)
