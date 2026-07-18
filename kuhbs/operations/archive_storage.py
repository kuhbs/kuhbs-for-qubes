# Purpose: Own the mounted backup-storage contract shared by archive commands
# Scope: Probe only the configured backup kuh; mounting remains backup_mount.py's job
from __future__ import annotations

from ..qubes import run_in_kuh, vm_running
from . import OperationContext


# Installed qrexec services hardcode this archive root and receive no defaults.yml
# Keep the matching Python values as one code contract, not user configuration
MOUNT_PATH = "/mnt"
BACKUP_PATH = "/mnt/kuhbs-backup"


def backup_vm_running(ctx: OperationContext, backup_kuh: str) -> bool:
    # Dom0 qvm-check is passive, so every caller can gate storage work without starting the VM
    return vm_running(ctx.runner, backup_kuh)


def require_backup_vm_running(ctx: OperationContext, backup_kuh: str) -> None:
    # A non-running autostart backup VM is exceptional and must be started explicitly by the operator
    if not backup_vm_running(ctx, backup_kuh):
        raise RuntimeError(f"Backup VM {backup_kuh} is not running; start it first")


def _running_backup_storage_status(ctx: OperationContext, backup_kuh: str) -> tuple[bool, bool]:
    # Callers reach guest mount probes only after one passive running-state admission
    # Return archive readiness and mount presence separately so the GUI can offer partial cleanup
    mounted = run_in_kuh(
        ctx.runner,
        backup_kuh,
        ["/usr/bin/mountpoint", "--quiet", MOUNT_PATH],
        user="root",
        check=False,
    )
    if mounted.returncode != 0:
        # Without the removable mount, a leftover archive directory is irrelevant
        return False, False
    archive_directory = run_in_kuh(
        ctx.runner,
        backup_kuh,
        ["/usr/bin/test", "-d", BACKUP_PATH],
        user="root",
        check=False,
    )
    return archive_directory.returncode == 0, True


def backup_storage_status(ctx: OperationContext, backup_kuh: str) -> tuple[bool, bool]:
    # Passive callers report unavailable storage without qvm-run starting a halted backup VM
    if not backup_vm_running(ctx, backup_kuh):
        return False, False
    return _running_backup_storage_status(ctx, backup_kuh)


def required_backup_storage_status(ctx: OperationContext, backup_kuh: str) -> tuple[bool, bool]:
    # Active storage commands need an error, not passive unavailable status, when the VM is halted
    require_backup_vm_running(ctx, backup_kuh)
    return _running_backup_storage_status(ctx, backup_kuh)


def backup_storage_ready(ctx: OperationContext, backup_kuh: str) -> bool:
    # Archive operations require both the removable mount and its archive directory
    ready, _mounted = backup_storage_status(ctx, backup_kuh)
    return ready


def require_backup_storage(ctx: OperationContext, backup_kuh: str) -> None:
    # Archive operations must fail before workers start when VM or removable storage is unavailable
    ready, _mounted = required_backup_storage_status(ctx, backup_kuh)
    if not ready:
        raise RuntimeError(
            f"Backup storage is not mounted at {MOUNT_PATH} "
            f"with archive directory {BACKUP_PATH} in {backup_kuh}"
        )


def backup_mapper_active(ctx: OperationContext, backup_kuh: str, crypt_name: str) -> bool:
    # External mounts have no KUHBS crypt mapper and must not trigger a bogus close
    result = run_in_kuh(
        ctx.runner,
        backup_kuh,
        ["/usr/bin/test", "-e", f"/dev/mapper/{crypt_name}"],
        user="root",
        check=False,
    )
    return result.returncode == 0
