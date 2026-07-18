# Purpose: Build and print state-aware plans for multi-target commands
# Scope: KUHB/dom0 gates use state files; Upgrade also carries configured base TemplateVMs
from __future__ import annotations

from dataclasses import dataclass
from ..model import action_allowed, base_template_vm_names
from . import OperationContext, definitions_by_id
from . import backup


@dataclass(frozen=True)
class Plan:
    # Runnable definitions are the only items operation modules receive after planning
    # Skipped entries block exact requests and stay visible during tolerant -all expansion
    runnable: tuple[dict, ...]
    skipped: tuple[tuple[str, str], ...]
    ordered: bool = False
    # Only shared all-plans need a clear nonzero message when no target exists
    empty_message: str | None = None
    # External base TemplateVMs join only Upgrade plans and execute after dom0
    qubes_templates: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        # A plan with only skipped items should print reasons and exit without prompting
        return not self.runnable and not self.qubes_templates

    @property
    def can_run_exact(self) -> bool:
        # Explicit CLI requests and GUI selections require every requested target
        return not self.is_empty() and not self.skipped


def broken_reasons(broken_kuhbs) -> dict[str, str]:
    # Keep malformed linked definitions in the same target/reason shape as ordinary blockers
    return {
        broken.active_id: "; ".join(f"{issue.path}: {issue.message}" for issue in broken.issues)
        for broken in broken_kuhbs
    }


def _state_reason(
    ctx: OperationContext,
    kuhb_id: str,
    action: str,
    definition: dict | None,
    broken_reasons: dict[str, str],
    states: dict[str, str] | None,
) -> str | None:
    # CLI callers read fresh gates while GUI callers supply the same facts from their status cache
    if kuhb_id in broken_reasons:
        return f"configuration broken: {broken_reasons[kuhb_id]}"
    if definition is None:
        return "unknown kuhb"
    # Archive participation is configuration policy, so GUI and CLI must reject it identically
    if action == "backup" and kuhb_id == "dom0" and not backup.dom0_backup_paths(ctx):
        return "no archive paths configured"
    if action in {"backup", "restore"} and kuhb_id != "dom0" and not backup.backup_enabled_kuhs(ctx, definition):
        return "no archive paths configured"
    if states is None:
        state = ctx.state_store.current_gate(kuhb_id)
    else:
        state = states.get(kuhb_id, "dom0" if kuhb_id == "dom0" else "linked")
    if action_allowed(state, action):
        return None
    return f"state is {state}"


def build_definition_plan(
    ctx: OperationContext,
    definitions: tuple[dict, ...],
    requested_ids: list[str],
    action: str,
    *,
    ordered: bool = False,
    reverse: bool = False,
    broken_reasons: dict[str, str] | None = None,
    states: dict[str, str] | None = None,
) -> Plan:
    # Keep duplicate CLI args harmless; the first mention defines the visible order
    known = definitions_by_id(definitions)
    seen: set[str] = set()
    runnable: list[dict] = []
    skipped: list[tuple[str, str]] = []
    broken_reasons = broken_reasons or {}

    for kuhb_id in requested_ids:
        if kuhb_id in seen:
            continue
        seen.add(kuhb_id)
        reason = _state_reason(
            ctx,
            kuhb_id,
            action,
            known.get(kuhb_id),
            broken_reasons,
            states,
        )
        if reason is None:
            runnable.append(known[kuhb_id])
        else:
            skipped.append((kuhb_id, reason))

    if ordered:
        # Create runs low-order infrastructure first; remove runs the inverse order
        runnable.sort(key=lambda definition: (definition["order"], definition["id"]), reverse=reverse)
    return Plan(tuple(runnable), tuple(skipped), ordered=ordered)


def build_upgrade_plan(
    ctx: OperationContext,
    definitions: tuple[dict, ...],
    requested_ids: list[str],
    *,
    broken_reasons: dict[str, str] | None = None,
    states: dict[str, str] | None = None,
) -> Plan:
    # Partition external TemplateVM names before KUHB lifecycle planning so both namespaces stay explicit
    template_names = set(base_template_vm_names(definitions))
    broken_reasons = broken_reasons or {}
    # A reserved-name collision stays unresolvable until the invalid KUHB definition is fixed
    requested_templates = tuple(sorted({
        target
        for target in requested_ids
        if target in template_names and target not in broken_reasons
    }))
    requested_definitions = [
        target
        for target in requested_ids
        if target not in template_names or target in broken_reasons
    ]
    plan_definitions = definitions + (({"id": "dom0", "order": 999999},) if "dom0" in requested_definitions else ())
    plan = build_definition_plan(
        ctx,
        plan_definitions,
        requested_definitions,
        "upgrade",
        ordered=True,
        broken_reasons=broken_reasons,
        states=states,
    )
    return Plan(plan.runnable, plan.skipped, ordered=True, qubes_templates=requested_templates)


def build_action_plan(
    ctx: OperationContext,
    definitions: tuple[dict, ...],
    requested_ids: list[str],
    action: str,
    *,
    ordered: bool = False,
    reverse: bool = False,
    broken_reasons: dict[str, str] | None = None,
    states: dict[str, str] | None = None,
) -> Plan:
    # One target policy serves fresh CLI requests and cached GUI button sensitivity
    if action == "upgrade":
        return build_upgrade_plan(ctx, definitions, requested_ids, broken_reasons=broken_reasons, states=states)

    # Dom0 is a real archive target even though it has no KUHB definition file
    plan_definitions = definitions
    if action in {"backup", "restore"} and "dom0" in requested_ids:
        plan_definitions += ({"id": "dom0", "order": 999999},)

    return build_definition_plan(
        ctx,
        plan_definitions,
        requested_ids,
        action,
        ordered=ordered,
        reverse=reverse,
        broken_reasons=broken_reasons,
        states=states,
    )


def build_all_action_plan(
    ctx: OperationContext,
    definitions: tuple[dict, ...],
    broken_kuhbs,
    command: str,
    *,
    states: dict[str, str] | None = None,
) -> Plan:
    # All expands every candidate, then the ordinary action planner partitions possible work
    specs = {
        "create-all": ("create", True, False),
        "remove-all": ("remove", True, True),
        "upgrade-all": ("upgrade", True, False),
        "backup-all": ("backup", False, False),
        "restore-all": ("restore", False, False),
    }
    if command not in specs:
        raise ValueError(f"unsupported all-action plan: {command}")
    action, ordered, reverse = specs[command]
    # Broken definitions remain visible as impossible candidates instead of blocking valid siblings
    requested = [definition["id"] for definition in definitions]
    requested.extend(broken.active_id for broken in broken_kuhbs)
    # Dom0 and base TemplateVMs are synthetic candidates added by their supported actions
    if command in {"backup-all", "restore-all", "upgrade-all"}:
        requested.append("dom0")
    if command == "upgrade-all":
        requested.extend(base_template_vm_names(definitions))
    plan = build_action_plan(
        ctx,
        definitions,
        requested,
        action,
        ordered=ordered,
        reverse=reverse,
        broken_reasons=broken_reasons(broken_kuhbs),
        states=states,
    )
    return Plan(
        plan.runnable,
        plan.skipped,
        ordered=plan.ordered,
        empty_message="No eligible targets",
        qubes_templates=plan.qubes_templates,
    )


def print_plan(plan: Plan, *, show_runnable: bool = True) -> None:
    # The terminal plan is intentionally plain so it remains readable in dom0 xterms
    # Every empty all-plan needs the explicit nonzero summary; skipped details explain why it is empty
    if plan.is_empty() and plan.empty_message:
        print(plan.empty_message)
    if plan.skipped:
        print("cannot run:")
        for kuhb_id, reason in plan.skipped:
            print(f"  {kuhb_id}: {reason}")
    # A blocked explicit request must not label eligible siblings as work that will run
    if show_runnable and (plan.runnable or plan.qubes_templates):
        print("will run:")
        if plan.ordered:
            current_order = None
            names: list[str] = []
            for definition in plan.runnable:
                order = definition["order"]
                if current_order is not None and order != current_order:
                    print(f"  order {current_order}: {', '.join(names)}")
                    names = []
                current_order = order
                names.append(definition["id"])
            if current_order is not None:
                print(f"  order {current_order}: {', '.join(names)}")
        elif plan.runnable:
            print(f"  {', '.join(definition['id'] for definition in plan.runnable)}")
        if plan.qubes_templates:
            print(f"  Qubes OS VMs: {', '.join(plan.qubes_templates)}")
