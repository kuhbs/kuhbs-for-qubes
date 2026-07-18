# Purpose: Restore operation from KUHBS backup archives
# Scope: Operation state is written by archive_targets.py around the restore batch
from __future__ import annotations

from shlex import quote

from ..model import Kuh
from ..qubes import restore_vm_power_state, run_in_kuh, vm_exists, vm_power_state
from . import OperationContext
from .archive_storage import (
    BACKUP_PATH,
    backup_storage_ready,
    require_backup_storage,
)
from .archive_targets import ArchiveRequest, ArchiveTarget, Dom0ArchiveTarget, run_targets, temporary_qrexec_policy, validate_existing_kuhs
from .backup import DOM0_ARCHIVE


def dom0_restore_stream_command(backup_kuh: str, archive: str) -> str:
    # Qubes cannot copy files into dom0, so dom0 restore streams the archive through pass-io
    backup_command = f"cat {quote(archive)}"
    return "set -o pipefail; " + " | ".join(
        [
            f"qvm-run --pass-io --user root {quote(backup_kuh)} {quote(backup_command)}",
            # Dom0 restore may include root-owned paths configured by the user
            "sudo tar -I zstd -xpf - -C /",
        ]
    )


def _archive_present(ctx: OperationContext, backup_kuh: str, archive: str) -> bool:
    # Root owns mounted archive media, so every caller uses the same permission context
    result = run_in_kuh(
        ctx.runner,
        backup_kuh,
        ["test", "-f", archive],
        user="root",
        check=False,
    )
    return result.returncode == 0


def _preflight_restore_targets(
    ctx: OperationContext,
    targets: list[ArchiveRequest],
) -> None:
    # Refuse the complete request before the first archive extraction changes data
    if not targets:
        return
    backup_kuh = ctx.defaults["backup"]["kuh"]
    if not vm_exists(ctx.runner, backup_kuh):
        message = f"BACKUP VM {backup_kuh} DOES NOT EXIST! ABORTING RESTORE!"
        ctx.logger.error("restore", message)
        raise RuntimeError(message)
    require_backup_storage(ctx, backup_kuh)
    vm_targets = [target for target in targets if isinstance(target, ArchiveTarget)]
    try:
        validate_existing_kuhs(
            ctx,
            [target.kuh for target in vm_targets],
        )
    except RuntimeError as exc:
        ctx.logger.error("restore", str(exc))
        raise
    for target in vm_targets:
        kuh = target.kuh
        archive = f"{BACKUP_PATH}/{kuh.name}.tar.zst"
        if not _archive_present(ctx, backup_kuh, archive):
            message = f"No backup found at {kuh.name}:{archive}"
            ctx.logger.error(target.kuhb_id, message)
            raise RuntimeError(message)
        if vm_power_state(ctx.runner, kuh.name) != "halted":
            message = f"Restore requires halted kuh: {kuh.name}"
            ctx.logger.error(target.kuhb_id, message)
            raise RuntimeError(message)
    if any(isinstance(target, Dom0ArchiveTarget) for target in targets):
        archive = f"{BACKUP_PATH}/{DOM0_ARCHIVE}"
        if not _archive_present(ctx, backup_kuh, archive):
            message = f"No dom0 backup found at {archive}"
            ctx.logger.error("dom0", message)
            raise RuntimeError(message)


def restore_kuh_archive(
    ctx: OperationContext,
    definition: dict,
    kuh: Kuh,
    *,
    was_running: bool | None = None,
    original_state: str | None = None,
) -> None:
    # Explicit restore preflight owns admission; this primitive only opens policy and extracts
    backup = ctx.defaults["backup"]
    backup_kuh = backup["kuh"]
    policy_path = f"/etc/qubes/policy.d/30-kuhbs-backup-read-{kuh.name}.policy"
    policy_line = f"kuhbs.BackupRead + {kuh.name} {backup_kuh} allow"
    restore_command = (
        f"set -o pipefail; qrexec-client-vm {quote(backup_kuh)} "
        "kuhbs.BackupRead | tar -I zstd -xpf - -C /"
    )
    # The trusted backup VM derives the archive from QREXEC_REMOTE_DOMAIN, so the target sends no filename
    try:
        with temporary_qrexec_policy(ctx, policy_path, policy_line):
            ctx.logger.status(definition["id"], f"Restoring {kuh.name}")
            run_in_kuh(ctx.runner, kuh.name, ["bash", "-lc", restore_command], user="root")
    finally:
        # Restore selected VM state even when extraction fails; the terminal log retains the failure evidence
        desired = original_state if original_state is not None else ("running" if was_running else "halted")
        restore_vm_power_state(ctx.runner, kuh.name, desired)


def restore_kuh_archive_if_present(ctx: OperationContext, definition: dict, kuh: Kuh) -> None:
    # Create-time restore is opportunistic and therefore owns its own availability checks
    backup_kuh = ctx.defaults["backup"]["kuh"]
    archive = f"{BACKUP_PATH}/{kuh.name}.tar.zst"
    if not vm_exists(ctx.runner, backup_kuh):
        message = f"BACKUP VM {backup_kuh} DOES NOT EXIST! SKIPPING RESTORE!"
        ctx.logger.warning(definition["id"], message)
        return
    if not backup_storage_ready(ctx, backup_kuh):
        message = (
            f"BACKUP STORAGE {BACKUP_PATH} IS NOT MOUNTED IN {backup_kuh}! "
            "SKIPPING RESTORE!"
        )
        ctx.logger.warning(definition["id"], message)
        return
    if not _archive_present(ctx, backup_kuh, archive):
        message = f"No backup found at {kuh.name}:{archive}"
        ctx.logger.warning(definition["id"], message)
        return
    original_state = vm_power_state(ctx.runner, kuh.name)
    if original_state == "paused":
        ctx.runner.run_for_kuh(kuh.name, ["qvm-unpause", kuh.name])
    restore_kuh_archive(ctx, definition, kuh, original_state=original_state)

def run_dom0(ctx: OperationContext) -> None:
    # Full-request preflight already checked the backup VM, storage and dom0 archive
    backup_kuh = ctx.defaults["backup"]["kuh"]
    archive = f"{BACKUP_PATH}/{DOM0_ARCHIVE}"
    ctx.logger.status("dom0", "Starting restore dom0")
    # Dom0 trusts the selected archive just like VM restore; the pipe fails if zstd or tar cannot read it
    ctx.runner.shell_for_dom0(dom0_restore_stream_command(backup_kuh, archive), visible_output=True)
    ctx.logger.status("dom0", "Restore completed")

def run_archive_target(ctx: OperationContext, target: ArchiveTarget) -> None:
    # Target construction and full-request preflight guarantee a concrete VM target
    restore_kuh_archive(ctx, target.definition, target.kuh, original_state="halted")

def run_archive_targets(ctx: OperationContext, targets: list[ArchiveRequest], *, source: str = "restore") -> None:
    # Preflight VM and dom0 archives together so no extraction starts on a partial request
    _preflight_restore_targets(ctx, targets)
    include_dom0 = any(isinstance(target, Dom0ArchiveTarget) for target in targets)
    vm_targets = [target for target in targets if isinstance(target, ArchiveTarget)]
    if vm_targets:
        run_targets(
            ctx,
            vm_targets,
            lambda target: run_archive_target(ctx, target),
            source=source,
            action="restore",
            success_phrase="restored",
            failure_phrase="restore",
        )
    if include_dom0:
        # Dom0 can replace repo/config files, so it runs alone after planned VM restores finish
        ctx.set_state("dom0", "restore", "start")
        try:
            run_dom0(ctx)
        except KeyboardInterrupt:
            # Synchronous dom0 restore has no parent executor, so it owns Ctrl+C process cleanup
            ctx.runner.kill_active_processes()
            ctx.set_state("dom0", "restore", "failed")
            raise
        except BaseException:
            ctx.set_state("dom0", "restore", "failed")
            raise
        # Final state output is outside the failure scope so an interrupted print cannot rewrite success as failed
        ctx.set_state("dom0", "restore", "completed")
