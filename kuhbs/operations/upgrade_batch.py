# Purpose: Ordered orchestration for KUHB, dom0 and Qubes base-TemplateVM upgrades
# Scope: Preserve KUHB order groups, then dom0, then one parallel base-template group
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from . import OperationContext
from . import upgrade


@dataclass(frozen=True)
class UpgradeFailure:
    # Store the user-visible target id with the original exception for the order summary
    target_id: str
    error: Exception

def _run_group(ctx: OperationContext, definitions: list[dict]) -> tuple[list[UpgradeFailure], upgrade.RestartPlan]:
    # Same-order failures are collected so sibling workers can finish before the order aborts
    failures: list[UpgradeFailure] = []
    restart_plan = upgrade.RestartPlan()
    batch_size = ctx.defaults["batch_size"]["upgrade"]
    with ThreadPoolExecutor(max_workers=min(batch_size, len(definitions))) as executor:
        futures = {executor.submit(upgrade.run, ctx, definition): definition for definition in definitions}
        try:
            for future in as_completed(futures):
                definition = futures[future]
                try:
                    result = future.result()
                    # Per-kuhb workers cannot safely prompt inside the thread pool, so their plans are merged here
                    upgrade._merge_restart_plans(restart_plan, result)
                except Exception as exc:
                    ctx.logger.error(definition["id"], f"Upgrade failed; finishing this order: {exc}")
                    failures.append(UpgradeFailure(definition["id"], exc))
        except KeyboardInterrupt:
            # Cancel queued work, kill active child groups and let workers unwind cleanly
            for future in futures:
                future.cancel()
            ctx.runner.kill_active_processes()
            raise RuntimeError("upgrade interrupted; killed active KUHBS commands")
    return failures, restart_plan


def _failure_summary(failures: list[UpgradeFailure]) -> str:
    # Keep each target failure readable when apt/qvm errors are long
    return "\n".join(f"{failure.target_id}: {failure.error}" for failure in failures)


def _confirm_network(ctx: OperationContext, definitions: list[dict]) -> None:
    # Network kuhs can need manual recovery after restart, e.g. typing the WiFi password in net-nic
    if not any(definition["confirm_network_after_upgrade"] is True for definition in definitions):
        return
    input("Reconnect/verify network, then press Enter to continue ")


def _run_qubes_templates(ctx: OperationContext, vm_names: tuple[str, ...]) -> list[UpgradeFailure]:
    # Independent Qubes base templates share the configured Upgrade worker limit
    failures: list[UpgradeFailure] = []
    if not vm_names:
        return failures
    batch_size = ctx.defaults["batch_size"]["upgrade"]
    with ThreadPoolExecutor(max_workers=min(batch_size, len(vm_names))) as executor:
        futures = {executor.submit(upgrade.upgrade_qubes_template_vm, ctx, vm_name): vm_name for vm_name in vm_names}
        try:
            for future in as_completed(futures):
                vm_name = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    ctx.logger.error(vm_name, f"Upgrade failed; finishing Qubes OS VM batch: {exc}")
                    failures.append(UpgradeFailure(vm_name, exc))
        except KeyboardInterrupt:
            for future in futures:
                future.cancel()
            ctx.runner.kill_active_processes()
            raise RuntimeError("upgrade interrupted; killed active KUHBS commands")
    return failures


def run_targets(
    ctx: OperationContext,
    definitions: list[dict],
    *,
    upgrade_dom0: bool = False,
    qubes_templates: tuple[str, ...] = (),
) -> None:
    # Order is dependency shape: lower-order infrastructure finishes before higher-order app kuhbs start
    ordered = sorted(definitions, key=lambda definition: definition["order"])
    index = 0
    while index < len(ordered):
        order = ordered[index]["order"]
        group: list[dict] = []
        # Build one equal-order batch; the next order waits until every worker in this group completed
        while index < len(ordered) and ordered[index]["order"] == order:
            group.append(ordered[index])
            index += 1
        ctx.logger.status("kuhbs", f"Starting upgrade order {order}: {', '.join(definition['id'] for definition in group)}")
        group_failures, restart_plan = _run_group(ctx, group)
        if restart_plan.restart or restart_plan.restart_paused or restart_plan.shutdown_only:
            try:
                # Listed infrastructure restarts without a prompt, but failures still abort this order
                no_prompt = set(ctx.defaults["upgrade"]["restart_without_prompt"])
                auto_restart = upgrade.RestartPlan(
                    restart=[vm_name for vm_name in restart_plan.restart if vm_name in no_prompt],
                    restart_paused=[vm_name for vm_name in restart_plan.restart_paused if vm_name in no_prompt],
                )
                prompted_plan = upgrade.RestartPlan(
                    restart=[vm_name for vm_name in restart_plan.restart if vm_name not in no_prompt],
                    restart_paused=[vm_name for vm_name in restart_plan.restart_paused if vm_name not in no_prompt],
                    shutdown_only=restart_plan.shutdown_only,
                )
                if auto_restart.restart or auto_restart.restart_paused:
                    upgrade._apply_restart_plan(ctx, "kuhbs", auto_restart)
                # Declining remaining restarts is not an order failure; later groups and dom0 still continue
                if upgrade._confirm_restart(ctx, "kuhbs", prompted_plan):
                    # Apply restarts before the order barrier so qvm-shutdown/qvm-start failures join the same report
                    upgrade._apply_restart_plan(ctx, "kuhbs", prompted_plan)
            except Exception as exc:
                group_failures.append(UpgradeFailure("restart", exc))
        if group_failures:
            # Print failures before raising so the terminal log shows why later orders and dom0 were skipped
            ctx.logger.error("kuhbs", f"Upgrade order failed:\n{_failure_summary(group_failures)}")
            raise RuntimeError(f"upgrade failed for:\n{_failure_summary(group_failures)}")
        _confirm_network(ctx, group)
    if upgrade_dom0:
        # Dom0 has no KUHB update timestamp, so its state file owns GUI health.
        ctx.set_state("dom0", "upgrade", "start")
        try:
            # dom0 intentionally stays after every KUHB order and before Qubes base templates
            ctx.logger.status("dom0", "Starting dom0 upgrade")
            ctx.runner.run_for_dom0(["sudo", "qubes-dom0-update"], visible_output=True)
        except BaseException:
            ctx.set_state("dom0", "upgrade", "failed")
            raise
        ctx.set_state("dom0", "upgrade", "completed")
    template_failures = _run_qubes_templates(ctx, qubes_templates)
    if template_failures:
        ctx.logger.error("kuhbs", f"Qubes OS VM upgrades failed:\n{_failure_summary(template_failures)}")
        raise RuntimeError(f"upgrade failed for:\n{_failure_summary(template_failures)}")
