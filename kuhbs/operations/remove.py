# Purpose: Remove operation for kuhb-managed Qubes resources
# Scope: Absent VMs are skipped after qvm-check --quiet probes so cleanup stays idempotent
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from ..hooks import run_hooks
from ..launchers import remove_launchers
from ..model import REMOVE_ORDER, resolve_kuhs
from ..qubes import kill_if_running, run_in_kuh, vm_exists
from . import OperationContext
from .upgrade import affected_running_dependents, remove_kuh_upgrade_timestamp


@dataclass(frozen=True)
class SnitchRuleDecision:
    # Cleanup decisions are immutable because every answer is collected before the first policy deletion
    definition_id: str
    rule_name: str
    snitch_vm: str
    rules_path: str
    remove: bool


def _snitch_rule_names(definition: dict) -> tuple[str, ...]:
    # Snitch stores direct KUH policy by VM name and stable UDP policy by dispvm-<app> identity
    names: list[str] = []
    for kuh in resolve_kuhs(definition):
        names.append(kuh.name)
        if kuh.kind == "app" and kuh.config["prefs"]["template_for_dispvms"] is True:
            names.append(f"dispvm-{kuh.name}")
    return tuple(names)


def _prompt_snitch_rule_decision(
    ctx: OperationContext,
    definition: dict,
    rule_name: str,
    snitch_vm: str,
    rules_dir: str,
) -> SnitchRuleDecision | None:
    rules_path = f"{rules_dir}/{rule_name}.yml"
    if run_in_kuh(ctx.runner, snitch_vm, ["test", "-f", rules_path], user="root", check=False, log=False).returncode != 0:
        return None
    while True:
        answer = input(f"Remove Qubes Snitch rules for {rule_name}? [y/n] ").strip().lower()
        if answer in {"y", "yes", "n", "no"}:
            return SnitchRuleDecision(
                definition_id=definition["id"],
                rule_name=rule_name,
                snitch_vm=snitch_vm,
                rules_path=rules_path,
                remove=answer in {"y", "yes"},
            )


def _collect_snitch_rule_decisions(ctx: OperationContext, definitions: tuple[dict, ...]) -> tuple[SnitchRuleDecision, ...]:
    # The CLI has already shown the admitted plan; collect every answer before parallel removal begins
    qubes_snitch = ctx.defaults["firewall"]["qubes_snitch"]
    snitch_vm = qubes_snitch["snitch_vm"]
    if not vm_exists(ctx.runner, snitch_vm):
        return ()
    rules_dir = qubes_snitch["rules_dir"]
    decisions: list[SnitchRuleDecision] = []
    for definition in definitions:
        for rule_name in _snitch_rule_names(definition):
            decision = _prompt_snitch_rule_decision(ctx, definition, rule_name, snitch_vm, rules_dir)
            if decision is not None:
                decisions.append(decision)
    return tuple(decisions)


def _apply_snitch_rule_decisions(ctx: OperationContext, decisions: tuple[SnitchRuleDecision, ...]) -> None:
    # Stored answers keep worker prompts and main-thread policy mutations from interleaving
    removed = False
    for decision in decisions:
        if decision.remove:
            ctx.logger.status(decision.definition_id, f"Removing Qubes Snitch rules for {decision.rule_name}")
            run_in_kuh(ctx.runner, decision.snitch_vm, ["rm", "-f", decision.rules_path], user="root")
            removed = True
        else:
            ctx.logger.warning(decision.definition_id, f"Keeping Qubes Snitch rules for removed VM identity {decision.rule_name}")
    if removed:
        # One daemon restart publishes every approved deletion without repeated policy reloads
        run_in_kuh(ctx.runner, decisions[0].snitch_vm, ["systemctl", "restart", "qubes-snitchd.service"], user="root")


def _kill_related_runtime_dispvms(ctx: OperationContext, definitions: tuple[dict, ...]) -> None:
    # Reuse Upgrade's trusted template-chain snapshot so Remove does not invent a second UDP resolver
    managed_names = {
        kuh.name
        for definition in definitions
        for kuh in resolve_kuhs(definition)
    }
    plan = affected_running_dependents(ctx, managed_names)
    for vm_name in plan.shutdown_only:
        # Runtime disposables cannot shut down cleanly after their template chain starts disappearing
        ctx.logger.status("remove", f"Killing related unnamed DisposableVM {vm_name}")
        ctx.runner.run_for_kuh(vm_name, ["qvm-kill", vm_name])


def _ordered_groups(definitions: tuple[dict, ...]) -> tuple[tuple[int, tuple[dict, ...]], ...]:
    # One shared order keeps the sequential questions aligned with later reverse-order execution
    by_order: dict[int, list[dict]] = {}
    for definition in definitions:
        by_order.setdefault(definition["order"], []).append(definition)
    return tuple(
        (order, tuple(sorted(by_order[order], key=lambda definition: definition["id"])))
        for order in sorted(by_order, reverse=True)
    )


def _run_definition(ctx: OperationContext, definition: dict) -> None:
    # Planning already admitted this KUHB; the worker owns the visible operation state
    ctx.register_kuh_labels(definition)
    kuhb_id = definition["id"]
    ctx.set_state(kuhb_id, "remove", "start")
    try:
        ctx.logger.status(kuhb_id, f"Starting remove {kuhb_id}")
        kuhs = resolve_kuhs(definition)
        by_kind = {kind: [kuh for kuh in kuhs if kuh.kind == kind] for kind in REMOVE_ORDER}
        for kind in REMOVE_ORDER:
            for kuh in by_kind.get(kind, []):
                if not vm_exists(ctx.runner, kuh.name):
                    # qvm-check makes remove idempotent after partial failures or manual cleanup
                    ctx.logger.status(kuhb_id, f"Skipping absent {kuh.name}")
                    remove_kuh_upgrade_timestamp(ctx, kuh.name)
                    continue
                kill_if_running(ctx.runner, kuh.name)
                # Remove hooks remain meaningful only while the target exists
                run_hooks(ctx, definition, kuh, "remove", location="dom0")
                ctx.logger.status(kuhb_id, f"Removing {kuh.name}")
                ctx.runner.run_for_kuh(kuh.name, ["qvm-remove", "--force", kuh.name])
                remove_kuh_upgrade_timestamp(ctx, kuh.name)
        if "sta" in definition["kuhs"]:
            tmp_name = f"tpl-{kuhb_id}-setup-tmp"
            if vm_exists(ctx.runner, tmp_name):
                kill_if_running(ctx.runner, tmp_name)
                ctx.logger.status(kuhb_id, f"Removing {tmp_name}")
                ctx.runner.run_for_kuh(tmp_name, ["qvm-remove", "--force", tmp_name])
        remove_launchers(ctx, definition)
    except BaseException:
        # The parent batch kills shared child processes on Ctrl+C; ordinary worker failures stay isolated
        ctx.set_state(kuhb_id, "remove", "failed")
        raise
    # Final state output is outside the failure scope so an interrupted print cannot rewrite success as failed
    ctx.set_state(kuhb_id, "remove", "completed")


def run(ctx: OperationContext, definition: dict) -> None:
    definitions = (definition,)
    # Questions follow the visible plan but precede every qvm-kill/qvm-remove mutation
    decisions = _collect_snitch_rule_decisions(ctx, definitions)
    _kill_related_runtime_dispvms(ctx, definitions)
    _run_definition(ctx, definition)
    # Cleanup follows successful removal without changing the existing lifecycle contract
    _apply_snitch_rule_decisions(ctx, decisions)


def run_all(ctx: OperationContext, definitions: tuple[dict, ...]) -> None:
    groups = _ordered_groups(definitions)
    # Flatten reverse-order groups once so questions and execution use one stable plan
    ordered_definitions = tuple(definition for _order, group in groups for definition in group)
    decisions = _collect_snitch_rule_decisions(ctx, ordered_definitions)
    batch_size = ctx.defaults["batch_size"]["remove"]
    for order, group in groups:
        # Scope UDP shutdown to this group so a failure cannot touch blocked later orders
        _kill_related_runtime_dispvms(ctx, group)
        failures: list[str] = []
        successful_ids: set[str] = set()
        with ThreadPoolExecutor(max_workers=min(batch_size, len(group))) as executor:
            futures = {
                executor.submit(_run_definition, ctx, definition): definition
                for definition in group
            }
            try:
                for future in as_completed(futures):
                    definition = futures[future]
                    try:
                        future.result()
                        successful_ids.add(definition["id"])
                    except Exception as exc:
                        ctx.logger.error(definition["id"], f"remove failed: {exc}")
                        failures.append(definition["id"])
            except KeyboardInterrupt:
                # Cancel queued work, kill active child groups and let the executor wait for worker state cleanup
                for future in futures:
                    future.cancel()
                ctx.runner.kill_active_processes()
                raise RuntimeError("remove interrupted; killed active KUHBS commands")
        # Main-thread cleanup keeps output ordered and touches only KUHBs whose workers returned success
        group_decisions = tuple(
            decision
            for decision in decisions
            if decision.definition_id in successful_ids
        )
        _apply_snitch_rule_decisions(ctx, group_decisions)
        if failures:
            # Later orders may rely on this order being gone, so stop after the failed group
            raise RuntimeError(f"remove failed at order {order}: {', '.join(sorted(failures))}")
