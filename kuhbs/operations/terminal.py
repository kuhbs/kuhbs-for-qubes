# Purpose: Open an interactive terminal in dom0 or a target VM
# Scope: dom0 only launches the terminal and returns control
from __future__ import annotations

import getpass
from shlex import join

from . import OperationContext
from ..qubes import vm_exists
from ..terminal import dom0_terminal_command_prefix, terminal_command_prefix


def _run_dom0_terminal(ctx: OperationContext, *, user: str) -> None:
    # dom0 terminals are local GUI processes; no qvm-run wrapper is needed
    ctx.logger.status("dom0", "Opening terminal in dom0")
    terminal_command = dom0_terminal_command_prefix(ctx, title="dom0: Terminal")
    if user == getpass.getuser():
        # Local interactive terminals outlive the CLI exactly like detached VM terminal launches
        ctx.runner.start(terminal_command, source="dom0", detached=True)
        return
    # Forward explicit dom0 user requests to sudo rather than silently ignoring the argument
    ctx.runner.start(["sudo", "--user", user, "--", *terminal_command], source="dom0", detached=True)


def run(
    ctx: OperationContext,
    kuh_name: str,
    *,
    user: str = "user",
    dispvm: bool = False,
    shutdown_on_exit_0: bool = False,
) -> None:
    # Run the requested operation while preserving KUHBS logging and error handling
    if kuh_name == "dom0":
        _run_dom0_terminal(ctx, user=user)
        return
    if not vm_exists(ctx.runner, kuh_name):
        raise RuntimeError(f"VM does not exist: {kuh_name}")
    ctx.logger.status(kuh_name, f"Opening terminal in {kuh_name}")
    # Interactive qvm-run terminals stay attached, so start qvm-run asynchronously and return the CLI
    terminal_command = terminal_command_prefix(
        ctx,
        kuh_name,
        title=f"{kuh_name}: Terminal",
        probe=not dispvm,
        lifetime_sensitive=shutdown_on_exit_0,
    )
    qvm_command = ["qvm-run", "--quiet", "--user", user]
    if dispvm:
        # qvm-run creates an unnamed disposable from the configured launcher target AppVM
        qvm_command.append("--dispvm")
    qvm_command.append(kuh_name)
    if shutdown_on_exit_0:
        # The guest shell waits for a clean terminal exit before shutting down the launched qube
        guest_command = f"{join(terminal_command)} && /usr/bin/sudo --non-interactive /sbin/shutdown -h now"
        qvm_command.extend(["/usr/bin/bash", "-lc", guest_command])
    else:
        qvm_command.extend(terminal_command)
    ctx.runner.start_for_kuh(kuh_name, qvm_command, detached=True)
