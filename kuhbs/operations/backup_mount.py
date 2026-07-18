# Purpose: Mount and unmount headerless encrypted USB backup storage
# Scope: Password entry and disk selection happen inside the backup kuh, never in dom0
from __future__ import annotations

from pathlib import Path

from ..config import resolve_path
from ..hooks import run_script_in_kuh_terminal
from ..qubes import run_in_kuh
from . import OperationContext
from .archive_storage import (
    BACKUP_PATH,
    MOUNT_PATH,
    backup_mapper_active,
    require_backup_vm_running,
    required_backup_storage_status,
)


def _backup_mount_script(ctx: OperationContext) -> Path:
    # The helper is an installed KUHBS setup script so backup-mount works without a repo checkout
    return resolve_path(ctx.defaults["paths"]["setup_scripts"]) / "kuhbs" / "backup-mount.sh"


def run_mount(ctx: OperationContext) -> None:
    # Never run the interactive opener on top of a partial mount or stale mapper
    backup_kuh = ctx.defaults["backup"]["kuh"]
    crypt_name = ctx.defaults["backup"]["crypt_name"]
    script = _backup_mount_script(ctx)
    # Mount is explicit storage work, but VM power remains an operator-owned prerequisite
    ready, mounted = required_backup_storage_status(ctx, backup_kuh)
    if ready:
        print(
            f"Backup storage is already mounted at {MOUNT_PATH} "
            f"with archive directory {BACKUP_PATH}; nothing to do"
        )
        return
    if mounted:
        raise RuntimeError(
            f"Backup storage is mounted at {MOUNT_PATH}, but archive directory "
            f"{BACKUP_PATH} is missing; run backup-umount before backup-mount"
        )
    if backup_mapper_active(ctx, backup_kuh, crypt_name):
        raise RuntimeError(
            f"Backup mapper {crypt_name} is already active without a mount at "
            f"{MOUNT_PATH}; run backup-umount before backup-mount"
        )
    print(f"Opening a terminal as root@{backup_kuh} to mount KUHBS backup storage")
    run_script_in_kuh_terminal(
        ctx,
        "kuhbs",
        backup_kuh,
        script,
        script_args=[MOUNT_PATH, BACKUP_PATH, crypt_name],
    )

    # The checked helper verifies the mounted archive directory before returning success
    print(f"Storage device has been mounted to {BACKUP_PATH}")


def _close_mapper_if_active(ctx: OperationContext, backup_kuh: str, crypt_name: str) -> bool:
    # External mounts do not own the configured mapper, so only close one that exists
    if not backup_mapper_active(ctx, backup_kuh, crypt_name):
        return False
    run_in_kuh(
        ctx.runner,
        backup_kuh,
        ["/usr/sbin/cryptsetup", "close", crypt_name],
        user="root",
    )
    return True


def run_unmount(ctx: OperationContext) -> None:
    # Cleanup cares only whether /mnt is mounted; the archive directory is irrelevant
    backup_kuh = ctx.defaults["backup"]["kuh"]
    crypt_name = ctx.defaults["backup"]["crypt_name"]
    # Umount shares the same explicit-running prerequisite as Mount, Backup, and Restore
    require_backup_vm_running(ctx, backup_kuh)
    mounted = run_in_kuh(
        ctx.runner,
        backup_kuh,
        ["/usr/bin/mountpoint", "--quiet", MOUNT_PATH],
        user="root",
        check=False,
    )
    if mounted.returncode == 0:
        # Checked umount flushes this filesystem and fails before mapper cleanup when busy
        run_in_kuh(ctx.runner, backup_kuh, ["/usr/bin/umount", MOUNT_PATH], user="root")
        _close_mapper_if_active(ctx, backup_kuh, crypt_name)
        print("Storage device has been unmounted")
        return
    if _close_mapper_if_active(ctx, backup_kuh, crypt_name):
        # A failed backup-mount can leave the mapper open before /mnt is mounted
        print("Storage mapper has been closed")
        return
    print(
        f"Backup storage is not mounted at {MOUNT_PATH} "
        f"with archive directory {BACKUP_PATH}; nothing to do"
    )
