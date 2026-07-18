# Purpose: Upgrade created KUHs and external Qubes base TemplateVMs
# Scope: KUHB upgrades retain hooks/restarts; base TemplateVMs use their fixed Qubes/APT sequence
# KUHBS does not use Qubes pending-update state to decide if a requested upgrade should run
# Qubes pending-update is a cached dom0 hint for GUI notifications, not KUHBS freshness state
# A TemplateVM can set that hint after it runs its own package-manager check
# A running AppVM can also set it for its parent TemplateVM after checking the AppVM root snapshot
# That is useful for Qubes update popups, including popups for KUHBS-managed TemplateVMs
# It does not fit KUHBS upgrade timing well
# KUHBS normally gives each application AppVM its own TemplateVM
# KUHBS does not usually base many AppVMs on one shared TemplateVM
# The exception is a kuhb that explicitly defines a list of app VMs
# This means a TemplateVM's application AppVM may not run for several days
# Qubes AppVM checks also run about five minutes after boot and then only about every two days
# Two days is much too long for the KUHBS upgrade signal
# KUHBS therefore prefers a simple local rule: request upgrades again after a fixed age
# The default age is defaults.yml upgrade.max_age_minutes, default 720 minutes, or 12 hours
# KUHBS writes its own timestamp after each kuh finishes its configured upgrade work
# Timestamps live in ~/.kuhbs/upgrades/<kuh>
# kuhbs list compares those timestamps with upgrade.max_age_minutes
# The list shows upgraded/upgradable plus how long ago the last successful upgrade happened
# We recommend running kuhbs upgrade-all directly after boot, before opening applications
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..config import resolve_path
from ..hooks import hook_paths, kuhb_dir, run_hooks, run_script_in_kuh_terminal
from ..model import Kuh, resolve_kuhs
from ..qubes import restart_vm, restore_vm_power_state, vm_exists, vm_power_state
from ..qubes_status import QubesStatus
from . import OperationContext


PACKAGE_UPGRADE_KINDS = {"tpl", "sta"}
HOOK_ONLY_KINDS = {"app"}
UPGRADE_HOOK_KINDS = PACKAGE_UPGRADE_KINDS | HOOK_ONLY_KINDS


def _apt_update_script(ctx: OperationContext):
    # One installed script keeps every KUHBS-triggered APT upgrade on the same Qubes policy
    path = resolve_path(ctx.defaults["paths"]["setup_scripts"]) / "kuhbs" / "setup" / "apt-update.sh"
    if not path.exists():
        raise FileNotFoundError(f"KUHBS apt update script missing: {path}")
    return path


def upgrade_qubes_template_vm(ctx: OperationContext, vm_name: str) -> None:
    # Qubes-owned base templates always follow the same halt, Qubes upgrade, APT, halt sequence
    ctx.runner.run_for_kuh(vm_name, ["qvm-shutdown", "--wait", "--force", vm_name])
    try:
        ctx.runner.run_for_kuh(vm_name, ["qvm-template", "upgrade", vm_name])
        run_script_in_kuh_terminal(ctx, vm_name, vm_name, _apt_update_script(ctx))
    finally:
        # A failed Qubes or APT upgrade must not leave the shared base TemplateVM running
        ctx.runner.run_for_kuh(vm_name, ["qvm-shutdown", "--wait", "--force", vm_name])


def upgrade_root(ctx: OperationContext):
    # Plain files make upgrade freshness inspectable and easy to reset from dom0
    return ctx.config_root / "upgrades"


def mark_kuh_upgrade_success(ctx: OperationContext, kuh_name: str) -> None:
    # Write only after this kuh's hooks and apt commands completed successfully
    upgrade_root(ctx).mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    (upgrade_root(ctx) / kuh_name).write_text(f"{timestamp}\n", encoding="utf-8")


def remove_kuh_upgrade_timestamp(ctx: OperationContext, kuh_name: str) -> None:
    # Removing a kuh also removes its freshness marker so recreated kuhs start as upgradable
    (upgrade_root(ctx) / kuh_name).unlink(missing_ok=True)


def last_kuh_upgrade(ctx: OperationContext, kuh_name: str) -> datetime | None:
    # Missing or malformed timestamps are stale, not upgraded
    path = upgrade_root(ctx) / kuh_name
    if not path.exists():
        return None
    try:
        timestamp = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if timestamp.tzinfo is None:
        return timestamp.astimezone()
    return timestamp


def _upgrade_age_label(timestamp: datetime | None) -> str:
    # Keep the list compact: minutes first, then whole hours
    if timestamp is None:
        return "never"
    age_seconds = max(0, int((datetime.now().astimezone() - timestamp).total_seconds()))
    age_minutes = age_seconds // 60
    if age_minutes < 60:
        return f"{age_minutes}m"
    return f"{age_minutes // 60}h"


def kuh_upgrade_status(ctx: OperationContext, kuh_name: str) -> str:
    # defaults.yml owns the freshness threshold so Python has no hidden fallback value
    timestamp = last_kuh_upgrade(ctx, kuh_name)
    age = _upgrade_age_label(timestamp)
    if timestamp is None:
        return f"upgradable:{age}"
    max_age = timedelta(minutes=float(ctx.defaults["upgrade"]["max_age_minutes"]))
    status = "upgraded" if datetime.now().astimezone() - timestamp <= max_age else "upgradable"
    return f"{status}:{age}"


def kuh_has_upgrade_work(ctx: OperationContext, definition: dict, kuh: Kuh) -> bool:
    # Package state belongs to tpl/sta; app kuhs count only when they define explicit update hooks
    return kuh.kind in PACKAGE_UPGRADE_KINDS or (kuh.kind in HOOK_ONLY_KINDS and _has_update_hook(ctx, definition, kuh))


@dataclass
class RestartPlan:
    # Persistent qubes return to their exact live state; runtime DispVMs cannot be recreated
    restart: list[str] = field(default_factory=list)
    restart_paused: list[str] = field(default_factory=list)
    shutdown_only: list[str] = field(default_factory=list)


def _is_runtime_dispvm(status: QubesStatus, vm_name: str) -> bool:
    # Qubes-generated runtime names are disp plus a decimal ID; named DispVMs remain persistent
    info = status.vm_info(vm_name)
    return (
        info is not None
        and info.klass == "DispVM"
        and vm_name.startswith("disp")
        and vm_name[4:].isdigit()
    )


def _template_chain(status: QubesStatus, vm_name: str) -> list[str]:
    # qubes.xml already contains template and dispvm_template parent links; avoid qvm-prefs per VM
    chain: list[str] = []
    seen = {vm_name}
    queue = [vm_name]
    while queue:
        current = queue.pop(0)
        info = status.vm_info(current)
        if info is None:
            continue
        for parent in (info.dispvm_template, info.template):
            if not parent or parent in seen:
                continue
            # Store every parent so upgraded app bases and upgraded templates both match dependents
            seen.add(parent)
            chain.append(parent)
            queue.append(parent)
    return chain


def _has_update_hook(ctx: OperationContext, definition: dict, kuh: Kuh) -> bool:
    # AppVMs are only touched when the kuhb author explicitly provided app update work
    return bool(hook_paths(kuhb_dir(definition, ctx), kuh.kind, kuh.instance_id, "update"))


def _add_unique(items: list[str], vm_name: str) -> None:
    # Preserve discovery order while avoiding duplicate shutdown/restart work.
    if vm_name not in items:
        items.append(vm_name)


def affected_running_dependents(ctx: OperationContext, changed: set[str], already_planned: set[str] | None = None) -> RestartPlan:
    # Share one Qubes relationship resolver with Remove so runtime disposable discovery cannot drift
    plan = RestartPlan()
    already_planned = already_planned or set()
    if not changed:
        return plan
    status = QubesStatus.load(ctx)
    for vm_name, state in status.states.items():
        if state not in {"running", "paused"} or vm_name in changed or vm_name in already_planned:
            continue
        if not (set(_template_chain(status, vm_name)) & changed):
            continue
        if _is_runtime_dispvm(status, vm_name):
            _add_unique(plan.shutdown_only, vm_name)
        elif state == "paused":
            _add_unique(plan.restart_paused, vm_name)
        else:
            _add_unique(plan.restart, vm_name)
    return plan


def _confirm_restart(ctx: OperationContext, kuhb_id: str, plan: RestartPlan) -> bool:
    # Prompt once for restartable VMs after upgrade work has finished
    if not plan.restart and not plan.restart_paused and not plan.shutdown_only:
        return False
    ctx.logger.status(kuhb_id, "Upgrade finished; running VMs need restart")
    # Batch orchestration owns the only prompt, so no worker-thread prompt lock is needed
    print("Upgrade finished.\n")
    if plan.restart:
        print("The following kuhs were running before the upgrade started. They are using snapshots")
        print("of the templates before the upgrade completed. In order for them to use the new")
        print("upgraded templates they need to be restarted:")
        print(", ".join(ctx.logger.source_name(vm_name) for vm_name in plan.restart))
        print()
    if plan.restart_paused:
        print("The following kuhs were paused before the upgrade started and will be paused again")
        print("after they restart on the upgraded template snapshot:")
        print(", ".join(ctx.logger.source_name(vm_name) for vm_name in plan.restart_paused))
        print()
    if plan.shutdown_only:
        print("The following unnamed disposable kuhs will just be shut down:")
        print(", ".join(ctx.logger.source_name(vm_name) for vm_name in plan.shutdown_only))
        print()
    print("Mind that running applications are not restarted automatically.")
    print()
    answer = input("Would you like to restart them now? [Y/n] ").strip().lower()
    return answer in {"", "y", "yes"}


def _apply_restart_plan(ctx: OperationContext, kuhb_id: str, plan: RestartPlan) -> None:
    # Restart or shut down the VMs selected by the confirmed restart plan
    for vm_name in plan.restart:
        ctx.logger.status(kuhb_id, f"Restarting {vm_name}")
        restart_vm(ctx.runner, vm_name)
    for vm_name in plan.restart_paused:
        ctx.logger.status(kuhb_id, f"Restarting paused {vm_name}")
        restart_vm(ctx.runner, vm_name, paused=True)
    for vm_name in plan.shutdown_only:
        ctx.logger.status(kuhb_id, f"Shutting down disposable {vm_name}")
        restore_vm_power_state(ctx.runner, vm_name, "halted")
        ctx.logger.info(kuhb_id, f"{vm_name} was stopped; relaunch the app if needed")


def _merge_restart_plans(target: RestartPlan, source: RestartPlan) -> None:
    # Append one restart plan into another without duplicating VM names
    for vm_name in source.restart:
        _add_unique(target.restart, vm_name)
    for vm_name in source.restart_paused:
        _add_unique(target.restart_paused, vm_name)
    for vm_name in source.shutdown_only:
        _add_unique(target.shutdown_only, vm_name)


def run(ctx: OperationContext, definition: dict) -> RestartPlan:
    # Upgrade touches only created kuhs, explicit app hooks, and discovered running dependents
    ctx.register_kuh_labels(definition)
    kuhb_id = definition["id"]
    kuhs = resolve_kuhs(definition)
    ctx.logger.status(kuhb_id, f"Starting upgrade {kuhb_id}")
    restart_plan = RestartPlan()
    upgraded_roots: set[str] = set()
    # One preflight snapshot avoids repeating adjacent qvm-check existence probes
    existing_before = {kuh.name: vm_exists(ctx.runner, kuh.name) for kuh in kuhs if kuh.kind in {"tpl", "app", "sta", "ndp"}}
    power_before = {kuh.name: vm_power_state(ctx.runner, kuh.name) for kuh in kuhs if existing_before.get(kuh.name)}
    for kuh in kuhs:
        if kuh.kind not in UPGRADE_HOOK_KINDS:
            # NDP package state comes from its app/template; preserve its live state across restart
            if kuh.kind == "ndp" and power_before.get(kuh.name) == "running":
                _add_unique(restart_plan.restart, kuh.name)
            elif kuh.kind == "ndp" and power_before.get(kuh.name) == "paused":
                _add_unique(restart_plan.restart_paused, kuh.name)
            continue
        if not existing_before.get(kuh.name):
            ctx.logger.warning(kuhb_id, f"Skipping absent {kuh.name}")
            continue
        if kuh.kind in HOOK_ONLY_KINDS and not _has_update_hook(ctx, definition, kuh):
            ctx.logger.info(kuhb_id, f"Skipping {kuh.name}; no update hook")
            continue
        ctx.logger.status(kuhb_id, f"Upgrading {kuh.name}")
        original_state = power_before[kuh.name]
        if original_state == "paused":
            ctx.runner.run_for_kuh(kuh.name, ["qvm-unpause", kuh.name])
        try:
            if kuh.kind in PACKAGE_UPGRADE_KINDS:
                run_script_in_kuh_terminal(ctx, kuhb_id, kuh.name, _apt_update_script(ctx))
                upgraded_roots.add(kuh.name)
            # Why: KUHB-specific migrations must see the package versions and preserved configuration produced by APT
            run_hooks(ctx, definition, kuh, "update")
            if kuh.kind in HOOK_ONLY_KINDS:
                upgraded_roots.add(kuh.name)
            mark_kuh_upgrade_success(ctx, kuh.name)
        finally:
            # Keep command failure evidence in the log without leaking a changed selected-VM power state
            restore_vm_power_state(ctx.runner, kuh.name, original_state)
        if original_state == "running":
            _add_unique(restart_plan.restart, kuh.name)
        elif original_state == "paused":
            _add_unique(restart_plan.restart_paused, kuh.name)
    already_planned = set(restart_plan.restart) | set(restart_plan.restart_paused)
    _merge_restart_plans(restart_plan, affected_running_dependents(ctx, upgraded_roots, already_planned))
    # The ordered batch owns restart aggregation, prompting and application for every CLI upgrade
    ctx.logger.status(kuhb_id, "Upgrade completed")
    return restart_plan
