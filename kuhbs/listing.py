# Purpose: Dom0-only status rendering for kuhbs list/ls
# Scope: Collect Qubes metadata without guest stdout probes
from __future__ import annotations

from dataclasses import dataclass, replace
import os
from shlex import quote
import sys
from typing import Iterable

from .command import CommandResult
from .model import Kuh, display_state, resolve_kuhs
from .operations import OperationContext
from .operations.archive_storage import BACKUP_PATH, backup_storage_ready
from .operations.backup import backup_paths_for_kuh
from .operations.upgrade import kuh_has_upgrade_work, kuh_upgrade_status
from .qubes_status import QubesStatus



LABEL_COLORS = {
    # Qubes labels map cleanly to ANSI colors; unknown custom labels stay uncolored
    "red": "\033[31m",
    "orange": "\033[38;5;208m",
    "yellow": "\033[33m",
    "green": "\033[32m",
    "gray": "\033[38;5;245m",
    "grey": "\033[38;5;245m",
    "blue": "\033[38;5;39m",
    "purple": "\033[35m",
    "black": "\033[30m",
}
RESET = "\033[0m"
BOLD = "\033[1m"
PURPLE = "\033[38;5;129m"
GREEN = "\033[32m"
ORANGE = "\033[38;5;208m"
RED = "\033[31m"
GRAY = "\033[38;5;245m"


@dataclass(frozen=True)
class KuhStatus:
    # One rendered status row for a persistent kuh or an unnamed disposable launcher target
    name: str
    status: str
    label: str
    update: str = ""
    backup: str = ""
    net_chain: tuple[str, ...] = ()
    net_labels: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class KuhbStatus:
    # Group rows by kuhb so the CLI mirrors the user's mental model
    kuhb_id: str
    display_name: str
    state: str
    kuhs: list[KuhStatus]


def _run(ctx: OperationContext, args: list[str], *, check: bool = False) -> CommandResult:
    # Status probes are read-only and should not pollute normal list output with COMMAND lines
    return ctx.runner.run(args, check=check, log=False)



def _color_enabled() -> bool:
    # Use ANSI colors only for terminal stdout, never for plain logs
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _paint(text: str, color: str) -> str:
    # Wrap text in one ANSI color and reset sequence when coloring is enabled
    if not color or not _color_enabled():
        return text
    return f"{color}{text}{RESET}"


def _state_color(state: str) -> str:
    # Map lifecycle state words to the color used in CLI list output
    if state in {"create:completed", "backup:completed", "restore:completed", "running", "paused"}:
        return GREEN
    if state in {"linked", "create:start", "backup:start", "restore:start", "remove:start"}:
        return ORANGE
    if state in {"halted", "launcher", "remove:completed"}:
        return GRAY
    return RED


def _configured_label(ctx: OperationContext, kuh: Kuh) -> str:
    # Missing VMs cannot be queried with qvm-prefs, so color them by the already-resolved config label
    merged = kuh.config
    label = merged["prefs"]["label"]
    return str(label).lower() if label else ""


def _plain_float(value: float) -> str:
    # Shell tools such as find are clearer and safer with decimal notation than Python's 1e-06 form
    return f"{value:.12f}".rstrip("0").rstrip(".")


def _backup_max_age_minutes(ctx: OperationContext) -> str:
    # Preserve fractional development thresholds instead of silently rounding them up to one minute
    hours = float(ctx.defaults["backup"]["max_age_hours"])
    minutes = hours * 60
    return _plain_float(minutes)


def _backup_age_label(ctx: OperationContext) -> str:
    # Keep list labels whole-numbered; the exact fractional value stays in find's threshold
    hours = float(ctx.defaults["backup"]["max_age_hours"])
    seconds = hours * 3600
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{int(hours)}h"


def _kuh_backup_archive(ctx: OperationContext, kuh: Kuh) -> str:
    # Backup archives are written by backup.py as one tarball per backup-enabled kuh
    return f"{BACKUP_PATH}/{kuh.name}.tar.zst"


def _kuh_backup_status(ctx: OperationContext, kuh: Kuh, storage_ready: bool) -> str:
    # List only probes kuhs whose YAML opts into backup; dom0 receives exit codes, not archive metadata
    if kuh.kind not in {"tpl", "app", "sta"} or backup_paths_for_kuh(kuh) is None:
        return ""
    if not storage_ready:
        # Inaccessible media cannot prove whether an archive exists. Keep list passive without
        # turning the backup VM on, but do not make a false recoverability claim.
        return "unavailable"
    backup_kuh = ctx.defaults["backup"]["kuh"]
    archive = _kuh_backup_archive(ctx, kuh)
    # Backup media may legitimately be root-owned. Match the access identity used by
    # readiness checks and archive operations instead of treating EACCES as absence.
    present = _run(ctx, ["qvm-run", "--quiet", "--user", "root", backup_kuh, "sh", "-c", f"test -f {quote(archive)}"])
    if present.returncode != 0:
        return "missing"
    minutes = _backup_max_age_minutes(ctx)
    # Keep the age comparison inside the backup kuh; test -n turns find's match/no-match into exit status
    recent = _run(ctx, ["qvm-run", "--quiet", "--user", "root", backup_kuh, "sh", "-c", f"test -n \"$(find {quote(archive)} -maxdepth 0 -type f -mmin -{minutes} -print -quit)\""])
    age_label = _backup_age_label(ctx)
    return f"recent:{age_label}" if recent.returncode == 0 else f"old:{age_label}"



def _backup_label(status: str) -> str:
    # Format backup status with the same wording as the CLI list output
    if status.startswith("recent:"):
        return f"<{status.split(':', 1)[1]}"
    if status.startswith("old:"):
        return f">{status.split(':', 1)[1]}"
    if status == "unavailable":
        return "unavailable"
    return "absent" if status == "missing" else ""

def _update_label(status: str) -> str:
    # Keep only the compact upgrade label shown in the table
    if not status:
        return ""
    state, _, age = status.partition(":")
    if state not in {"upgraded", "upgradable"}:
        return ""
    return f"{state}:{age}" if age else state


def _update_field(status: str, width: int) -> str:
    # Render the compact upgrade field for one KUHB row
    label = _update_label(status)
    if not label:
        return " " * width
    state = status.split(":", 1)[0]
    color = RED if state == "upgradable" else GREEN
    return _paint(f"{label:<{width}}", color)


def _backup_field(status: str, kuhb_state: str, width: int) -> str:
    # Missing backups are warnings before create/remove completion and errors after successful create
    label = _backup_label(status)
    if not label:
        return " " * width
    if status.startswith("recent:"):
        color = GREEN
    elif status.startswith("old:"):
        color = RED
    elif status == "unavailable":
        color = GRAY
    else:
        color = ORANGE if kuhb_state in {"linked", "remove:completed"} else RED
    return _paint(f"{label:<{width}}", color)



def _kuh_update_status(ctx: OperationContext, definition: dict, kuh: Kuh) -> str:
    # Show KUHBS' own last-successful-upgrade age, not Qubes' cached pending-update hint
    if not kuh_has_upgrade_work(ctx, definition, kuh):
        return ""
    return kuh_upgrade_status(ctx, kuh.name)


def _kuh_status(
    ctx: OperationContext,
    status_cache: QubesStatus,
    definition: dict,
    kuh: Kuh,
    storage_ready: bool,
) -> KuhStatus:
    # Combine cached Qubes data and KUHBS policy into one rendered row
    if kuh.kind == "udp":
        # UDP entries are launcher targets, not persistent qubes with Qubes metadata
        return KuhStatus(name=kuh.name, status="launcher", label="")
    info = status_cache.vm_info(kuh.name)
    if info is None:
        # qubes.xml is authoritative for expected persistent VM existence
        return KuhStatus(
            name=kuh.name,
            status="missing",
            label=_configured_label(ctx, kuh),
            backup=_kuh_backup_status(ctx, kuh, storage_ready),
        )
    status = status_cache.states.get(kuh.name, "halted")
    label = info.label
    update = _kuh_update_status(ctx, definition, kuh)
    net_chain = status_cache.netvm_chain(kuh.name)
    # Stale netvm prefs can name deleted qubes; keep the chain visible without crashing list
    net_labels = []
    for name in net_chain:
        net_info = status_cache.vm_info(name)
        net_labels.append((name, net_info.label if net_info else ""))
    return KuhStatus(
        name=kuh.name,
        status=status,
        label=label,
        update=update,
        backup=_kuh_backup_status(ctx, kuh, storage_ready),
        net_chain=net_chain,
        net_labels=tuple(net_labels),
    )


def _align_missing_with_kuhb_state(kuh: KuhStatus, kuhb_state: str) -> KuhStatus:
    # Missing VMs are visually absent before create or after remove completion
    if kuh.status == "missing" and kuhb_state in {"linked", "remove:completed"}:
        return replace(kuh, status="absent")
    return kuh


def collect_status(ctx: OperationContext, definitions: Iterable[dict]) -> list[KuhbStatus]:
    # Every probe is derived from startup-validated, merged local definitions
    statuses: list[KuhbStatus] = []
    status_cache = QubesStatus.load(ctx)
    source_definitions = list(definitions)
    resolved_definitions = [
        (definition, resolve_kuhs(definition))
        for definition in source_definitions
    ]
    has_backups = any(
        kuh.kind in {"tpl", "app", "sta"} and backup_paths_for_kuh(kuh) is not None
        for _definition, kuhs in resolved_definitions
        for kuh in kuhs
    )
    backup_kuh = ctx.defaults["backup"]["kuh"]
    # Passive status must not start a halted backup VM merely to inspect its mounted storage
    storage_ready = (
        has_backups
        and status_cache.states.get(backup_kuh) == "running"
        and backup_storage_ready(ctx, backup_kuh)
    )
    for definition, resolved_kuhs in resolved_definitions:
        kuhb_id = definition["id"]
        state = ctx.state_store.current_gate(kuhb_id)
        raw_kuhs = [
            _kuh_status(ctx, status_cache, definition, kuh, storage_ready)
            for kuh in resolved_kuhs
        ]
        kuhs = [_align_missing_with_kuhb_state(kuh, state) for kuh in raw_kuhs]
        statuses.append(
            KuhbStatus(
                kuhb_id=kuhb_id,
                display_name=definition["name"],
                state=state,
                kuhs=kuhs,
            )
        )
    return statuses


def _widths(statuses: list[KuhbStatus]) -> tuple[int, int, int, int]:
    # Compute table column widths from rendered rows before printing
    names = [status.kuhb_id for status in statuses] + [kuh.name for status in statuses for kuh in status.kuhs]
    states = [display_state(status.state) for status in statuses] + [kuh.status for status in statuses for kuh in status.kuhs]
    updates = [_update_label(kuh.update) for status in statuses for kuh in status.kuhs if kuh.update]
    backups = [_backup_label(kuh.backup) for status in statuses for kuh in status.kuhs if kuh.backup]
    return (
        max([len("KUHB/KUH"), *map(len, names)]),
        max([len("STATE"), *map(len, states)]),
        max([len("Upgrade"), *map(len, updates)]),
        max([len("BACKUP"), *map(len, backups)]),
    )


def _colored_kuh_name(kuh: KuhStatus, width: int) -> str:
    # Pad before coloring so ANSI escapes do not break column alignment
    padded = f"{kuh.name:<{width}}"
    return _paint(padded, LABEL_COLORS.get(kuh.label, "")) if kuh.label else padded


def _table_header(widths: tuple[int, int, int, int]) -> str:
    # Build the fixed CLI list header in the same column order as rows
    name_w, state_w, update_w, backup_w = widths
    header = "  ".join([
        f"{'KUHB/KUH':<{name_w}}",
        f"{'STATE':<{state_w}}",
        f"{'Upgrade':<{update_w}}",
        f"{'BACKUP':<{backup_w}}",
    ])
    return _paint(header.rstrip(), BOLD)


def _network_name(name: str, managed_labels: dict[str, str]) -> str:
    # Color a network-tree node when it belongs to a managed KUHB
    return _paint(name, LABEL_COLORS.get(managed_labels[name], ""))


def _render_tree_node(name: str, children: dict[str, set[str]], managed_labels: dict[str, str], prefix: str = "", seen: set[str] | None = None) -> list[str]:
    # Linux tree-style branches make netvm fanout readable without widening the status table
    seen = set() if seen is None else set(seen)
    if name in seen:
        return [f"{prefix}└── {_network_name(name, managed_labels)} [loop]"]
    seen.add(name)
    rows: list[str] = []
    ordered = sorted(children.get(name, set()))
    for index, child in enumerate(ordered):
        last = index == len(ordered) - 1
        branch = "└── " if last else "├── "
        rows.append(f"{prefix}{branch}{_network_name(child, managed_labels)}")
        extension = "    " if last else "│   "
        rows.extend(_render_tree_node(child, children, managed_labels, prefix + extension, seen))
    return rows


def _networking_tree(statuses: list[KuhbStatus]) -> str:
    # Only existing qubes belong in network shape; absent definitions may gain netvm later
    children: dict[str, set[str]] = {}
    managed_labels: dict[str, str] = {}
    roots: set[str] = set()
    no_network: list[str] = []
    for status in statuses:
        for kuh in status.kuhs:
            if kuh.status in {"absent", "missing", "launcher"}:
                continue
            managed_labels[kuh.name] = kuh.label
            for name, label in kuh.net_labels:
                managed_labels.setdefault(name, label)
            chain = list(kuh.net_chain)
            if not chain:
                no_network.append(kuh.name)
                continue
            path = list(reversed(chain)) + [kuh.name]
            roots.add(path[0])
            for parent, child in zip(path, path[1:]):
                children.setdefault(parent, set()).add(child)
    if not roots and not no_network:
        return ""
    lines = [_paint('Network chain', BOLD)]
    for root in sorted(roots):
        lines.append(_network_name(root, managed_labels))
        lines.extend(_render_tree_node(root, children, managed_labels))
        lines.append("")
    if no_network:
        names = ", ".join(_network_name(name, managed_labels) for name in sorted(no_network))
        lines.append(f"{_paint('No network kuh', BOLD)}: {names}")
    return "\n".join(lines).rstrip()


def render_status(statuses: list[KuhbStatus]) -> str:
    # Header cells carry labels so VM rows can stay compact and visually aligned
    widths = _widths(statuses)
    name_w, state_w, update_w, backup_w = widths
    lines: list[str] = []
    lines.append(_table_header(widths))
    lines.append("")
    for status in statuses:
        kuhb_name = _paint(f"{status.kuhb_id:<{name_w}}", f"{BOLD}{PURPLE}")
        state_label = display_state(status.state)
        state = _paint(f"{state_label:<{state_w}}", _state_color(status.state))
        lines.append(f"{kuhb_name}  {state}")
        for kuh in status.kuhs:
            power = _paint(f"{kuh.status:<{state_w}}", _state_color(kuh.status))
            fields = [
                _update_field(kuh.update, update_w),
                _backup_field(kuh.backup, status.state, backup_w),
            ]
            facts = "  ".join(fields).rstrip()
            suffix = f"  {facts}" if facts else ""
            lines.append(f"{_colored_kuh_name(kuh, name_w)}  {power}{suffix}")
        lines.append("")
    network_tree = _networking_tree(statuses)
    if network_tree:
        lines.extend([network_tree, ""])
    return "\n".join(lines).rstrip() + "\n"


def print_list(ctx: OperationContext, definitions: Iterable[dict]) -> None:
    # Rendering stays separate from collection so tests can inspect structured status.
    print(render_status(collect_status(ctx, definitions)), end="")
