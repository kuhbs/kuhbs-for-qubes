# Purpose: Small Qubes command helpers shared by operations
# Scope: Existence and running checks stay explicit before destructive calls
from __future__ import annotations

from .command import CommandResult, CommandRunner


def vm_exists(runner: CommandRunner, vm_name: str) -> bool:
    # qvm-check is the cheap, explicit guard before operations that may target absent kuhs
    result = runner.run_for_kuh(vm_name, ["qvm-check", "--quiet", vm_name], check=False)
    return result.returncode == 0


def vm_running(runner: CommandRunner, vm_name: str) -> bool:
    # qvm-kill should only run for live qubes; halted existing qubes can go straight to qvm-remove
    result = runner.run_for_kuh(vm_name, ["qvm-check", "--quiet", "--running", vm_name], check=False)
    return result.returncode == 0


def vm_paused(runner: CommandRunner, vm_name: str) -> bool:
    # Paused domains are live but qvm-check --running deliberately does not classify them as running
    result = runner.run_for_kuh(vm_name, ["qvm-check", "--quiet", "--paused", vm_name], check=False)
    return result.returncode == 0


def vm_power_state(runner: CommandRunner, vm_name: str) -> str:
    # Operations that temporarily start a selected VM must distinguish paused from halted
    if vm_paused(runner, vm_name):
        return "paused"
    if vm_running(runner, vm_name):
        return "running"
    return "halted"


def run_in_kuh(
    runner: CommandRunner,
    kuh_name: str,
    command: list[str],
    *,
    user: str | None = None,
    check: bool = True,
    log: bool = True,
) -> CommandResult:
    # Build qvm-run argv in one place so operation code only names target, user, and guest command
    # Wrap qvm-run argument construction so callers pass normal command argv lists
    args = ["qvm-run", "--quiet"]
    if user is not None:
        args.extend(["--user", user])
    args.extend([kuh_name, *command])
    return runner.run_for_kuh(kuh_name, args, check=check, log=log)


def kill_if_running(runner: CommandRunner, vm_name: str) -> None:
    # Destructive remove flow uses qvm-kill only when qvm-check confirms the VM is live
    if vm_running(runner, vm_name):
        runner.run_for_kuh(vm_name, ["qvm-kill", vm_name])


def shutdown_if_running(runner: CommandRunner, vm_name: str) -> None:
    # Delayed restart plans can outlive runtime DispVMs, so check live state before shutdown
    if vm_running(runner, vm_name):
        runner.run_for_kuh(vm_name, ["qvm-shutdown", "--wait", "--force", vm_name])


def restore_vm_power_state(runner: CommandRunner, vm_name: str, desired: str) -> None:
    # Restore the selected persistent VM exactly; external NetVM dependency state is intentionally out of scope
    current = vm_power_state(runner, vm_name)
    if current == desired:
        return
    if desired == "halted":
        if current == "paused":
            runner.run_for_kuh(vm_name, ["qvm-unpause", vm_name])
        shutdown_if_running(runner, vm_name)
        return
    if desired == "running":
        if current == "paused":
            runner.run_for_kuh(vm_name, ["qvm-unpause", vm_name])
        elif current == "halted":
            runner.run_for_kuh(vm_name, ["qvm-start", vm_name])
        return
    if desired == "paused":
        if current == "halted":
            runner.run_for_kuh(vm_name, ["qvm-start", vm_name])
        runner.run_for_kuh(vm_name, ["qvm-pause", vm_name])
        return
    raise ValueError(f"Unsupported VM power state: {desired}")


def restart_vm(runner: CommandRunner, vm_name: str, *, paused: bool = False) -> None:
    # A paused domain must be resumed before a checked graceful shutdown can complete
    if vm_paused(runner, vm_name):
        runner.run_for_kuh(vm_name, ["qvm-unpause", vm_name])
    shutdown_if_running(runner, vm_name)
    runner.run_for_kuh(vm_name, ["qvm-start", vm_name])
    if paused:
        runner.run_for_kuh(vm_name, ["qvm-pause", vm_name])
