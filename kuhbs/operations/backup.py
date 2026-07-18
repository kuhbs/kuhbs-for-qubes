# Purpose: Backup archive creation for created persistent kuhs
# Scope: VM backups use qrexec; dom0 backup still streams through dom0
from __future__ import annotations

from shlex import quote
from typing import cast

from ..hooks import run_hooks
from ..model import Kuh, resolve_kuhs
from ..qubes import restore_vm_power_state, run_in_kuh, vm_exists, vm_power_state
from . import OperationContext
from .archive_storage import BACKUP_PATH, require_backup_storage
from .archive_targets import ArchiveRequest, ArchiveTarget, Dom0ArchiveTarget, run_targets, temporary_qrexec_policy, validate_existing_kuhs


DOM0_ARCHIVE = "dom0.tar.zst"

def tar_create_args(output: str, *, ignore_failed_read: bool) -> list[str]:
    # Busy applications can opt into warnings while strict backups keep tar failures
    args = ["tar", "-I", "zstd -T0 -3", "-cpf", output]
    if ignore_failed_read:
        args.append("--ignore-failed-read")
    return args

def dom0_backup_paths(ctx: OperationContext) -> list[str]:
    # Startup validation owns the list shape and path policy; an empty list disables dom0 backup
    return [str(item) for item in ctx.defaults["backup"]["dom0_paths"]]


def _preflight_backup_targets(
    ctx: OperationContext,
    targets: list[ArchiveRequest],
) -> dict[str, str]:
    # Validate the complete request before any worker can replace an archive
    if not targets:
        return {}
    backup_kuh = ctx.defaults["backup"]["kuh"]
    if not vm_exists(ctx.runner, backup_kuh):
        raise RuntimeError(f"BACKUP VM {backup_kuh} DOES NOT EXIST! ABORTING BACKUP!")
    require_backup_storage(ctx, backup_kuh)
    vm_targets = [target for target in targets if isinstance(target, ArchiveTarget)]
    try:
        validate_existing_kuhs(
            ctx,
            [target.kuh for target in vm_targets],
        )
    except RuntimeError as exc:
        ctx.logger.error("backup", str(exc))
        raise

    # Reading power state does not start or unpause targets before their batch worker runs
    return {
        target.id: vm_power_state(ctx.runner, target.kuh.name)
        for target in vm_targets
    }


def run_dom0(ctx: OperationContext) -> None:
    # Tar reports missing configured sources through the checked pipeline
    backup = ctx.defaults["backup"]
    backup_kuh = backup["kuh"]
    ctx.logger.status("dom0", "Starting backup dom0")
    paths = dom0_backup_paths(ctx)
    tar_args = tar_create_args(
        "-",
        ignore_failed_read=backup["dom0_ignore_failed_read"],
    )
    source_command = " ".join(quote(arg) for arg in tar_args) + " -- " + " ".join(paths)
    archive = quote(f"{BACKUP_PATH}/{DOM0_ARCHIVE}")
    destination_command = f"umask 077 && cat - > {archive}"
    command = (
        f"set -o pipefail; {source_command} | "
        f"qvm-run --pass-io --user root {quote(backup_kuh)} "
        f"{quote(destination_command)}"
    )
    ctx.runner.shell_for_dom0(command)
    ctx.logger.status("dom0", "Backup completed")

def backup_paths_for_kuh(kuh) -> list[str] | None:
    # Per-kuh paths are trusted shell-expanded tar sources
    if "backup" not in kuh.config:
        return None
    paths = kuh.config["backup"]["paths"]
    # Startup validation guarantees backup.paths is a non-empty list of tar source patterns
    return [str(path) for path in paths]

def backup_enabled_kuhs(ctx: OperationContext, definition: dict):
    # YAML decides exactly which persistent kuhs participate in backup/restore
    return [
        kuh
        for kuh in resolve_kuhs(definition)
        if kuh.kind in {"tpl", "app", "sta"}
        and backup_paths_for_kuh(kuh) is not None
    ]

def run_kuh_archive(
    ctx: OperationContext,
    definition: dict,
    kuh: Kuh,
    *,
    was_running: bool | None = None,
    original_state: str | None = None,
) -> None:
    # Back up exactly one persistent kuh; batch code decides how many archives run in parallel
    backup = ctx.defaults["backup"]
    backup_kuh = backup["kuh"]
    kuh_backup = kuh.config["backup"]
    backup_paths = cast(list[str], kuh_backup["paths"])
    # Strict defaults catch missing sources; only the selected concrete VM can opt out
    ignore_failed_read = kuh_backup.get(
        "ignore_failed_read",
        backup["ignore_failed_read"],
    )
    policy_path = f"/etc/qubes/policy.d/30-kuhbs-backup-write-{kuh.name}.policy"
    policy_line = f"kuhbs.BackupWrite + {kuh.name} {backup_kuh} allow"
    tar_args = tar_create_args(
        "-",
        ignore_failed_read=ignore_failed_read,
    )
    source_command = " ".join(quote(arg) for arg in tar_args) + " -- " + " ".join(backup_paths)
    backup_command = f"set -o pipefail; {source_command} | qrexec-client-vm {quote(backup_kuh)} kuhbs.BackupWrite"
    # BackupWrite exists only while dom0 is intentionally running this backup
    # This prevents a compromised VM from overwriting its own archive later
    try:
        if original_state == "paused":
            # Defer unpausing until this worker owns the target under the backup batch limit
            ctx.runner.run_for_kuh(kuh.name, ["qvm-unpause", kuh.name])
        run_hooks(ctx, definition, kuh, "backup", location="kuh")
        with temporary_qrexec_policy(ctx, policy_path, policy_line):
            ctx.logger.status(kuh.name, f"Creating archive for {kuh.name}")
            run_in_kuh(ctx.runner, kuh.name, ["bash", "-lc", backup_command], user="root")
    finally:
        # Preserve halted/running/paused state after either a successful or failed stream
        desired = original_state if original_state is not None else ("running" if was_running else "halted")
        restore_vm_power_state(ctx.runner, kuh.name, desired)

def run_archive_target(
    ctx: OperationContext,
    target: ArchiveTarget,
    original_states: dict[str, str],
) -> None:
    # Full-request preflight has already admitted this VM target
    run_kuh_archive(
        ctx,
        target.definition,
        target.kuh,
        original_state=original_states[target.id],
    )

def run_archive_targets(ctx: OperationContext, targets: list[ArchiveRequest], *, source: str = "backup") -> None:
    # Preflight the complete target set before concurrent workers can replace archives
    original_states = _preflight_backup_targets(ctx, targets)
    vm_targets = [target for target in targets if isinstance(target, ArchiveTarget)]
    include_dom0 = any(isinstance(target, Dom0ArchiveTarget) for target in targets)
    if vm_targets:
        run_targets(
            ctx,
            vm_targets,
            lambda target: run_archive_target(ctx, target, original_states),
            source=source,
            action="backup",
            success_phrase="backed up",
            failure_phrase="back up",
        )
    if include_dom0:
        ctx.set_state("dom0", "backup", "start")
        try:
            # Dom0 runs synchronously after every VM archive completed successfully
            run_dom0(ctx)
        except KeyboardInterrupt:
            ctx.runner.kill_active_processes()
            ctx.set_state("dom0", "backup", "failed")
            raise RuntimeError("dom0 backup interrupted; killed active KUHBS commands")
        except BaseException:
            ctx.set_state("dom0", "backup", "failed")
            raise
        # Final state output is outside the failure scope so an interrupted print cannot rewrite success as failed
        ctx.set_state("dom0", "backup", "completed")
