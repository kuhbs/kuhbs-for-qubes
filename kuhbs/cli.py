# Purpose: KUHBS command-line entrypoint and argument dispatch
# Scope: Keep user errors short and operation code outside argparse
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from .config import resolve_path
from .listing import print_list
from .log import EventLogger
from .operations import OperationContext
from .operations import archive_targets, backup, backup_mount, create, remove, repository, restore, terminal, upgrade_batch
from .operations.planning import broken_reasons, build_action_plan, build_all_action_plan, print_plan
from .validation import ConfigValidationError, inspect_startup_config


CLI_MARKER = Path.home() / ".kuhbs/states/cli"
GUI_MARKER = Path.home() / ".kuhbs/states/gui"


def normalize_global_options(argv: list[str]) -> list[str]:
    # Accept documented globals before or after the command while leaving command-specific flags alone
    globals_out = []
    rest = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--defaults" and index + 1 < len(argv):
            globals_out.extend(argv[index:index + 2])
            index += 2
            continue
        if token.startswith("--defaults="):
            globals_out.append(token)
            index += 1
            continue
        rest.append(token)
        index += 1
    return globals_out + rest

def build_parser() -> argparse.ArgumentParser:
    # Argparse owns command syntax and help while operation modules own Qubes behavior
    parser = argparse.ArgumentParser(
        prog="kuhbs",
        usage="kuhbs [global options] <command> [command options]",
    )
    parser.add_argument("--defaults", type=Path, default=None, help="use another defaults.yml")

    sub = parser.add_subparsers(dest="command", title="commands", metavar="<command>")
    p = sub.add_parser("create", help="create selected KUHBs by order")
    p.add_argument("kuhb", nargs="+")
    p = sub.add_parser("remove", help="remove selected KUHB resources")
    p.add_argument("kuhb", nargs="+")
    p = sub.add_parser("backup", help="back up selected KUHBs or dom0")
    p.add_argument("target", nargs="+")
    p = sub.add_parser("restore", help="restore selected KUHBs or dom0")
    p.add_argument("target", nargs="+")
    sub.add_parser("create-all", help="create all local KUHBs")
    sub.add_parser("remove-all", help="remove all local KUHBs")
    sub.add_parser("backup-all", help="back up all local KUHBs and dom0")
    sub.add_parser("restore-all", help="restore all local KUHBs and dom0")
    sub.add_parser("backup-mount", help="unlock and mount backup storage")
    sub.add_parser("backup-umount", help="unmount and close backup storage")
    p = sub.add_parser("upgrade", help="upgrade selected KUHBs, configured Qubes TemplateVMs, or dom0")
    # One explicit command shares Upgrade-All ordering across KUHB, dom0 and base-TemplateVM targets
    p.add_argument("kuhb", nargs="+")
    sub.add_parser("upgrade-all", help="upgrade all local KUHBs, dom0 and configured Qubes TemplateVMs")
    p = sub.add_parser("terminal", help="open a terminal in one KUH")
    p.add_argument("kuh")
    # The target user is intentionally not validated in dom0; qvm-run reports missing VM users clearly
    p.add_argument("user", nargs="?", default="user")
    # Generated launchers forward the YAML behavior that differs from a plain persistent terminal
    p.add_argument("--dispvm", action="store_true", help="open the terminal in an unnamed DisposableVM")
    p.add_argument("--shutdown-on-exit-0", action="store_true", help="shut down the target after a clean terminal exit")
    p = sub.add_parser("repo-add", help="clone one audited repository checkout")
    p.add_argument("url")
    p.add_argument("branch", nargs="?")
    p = sub.add_parser("update-repo", help="update one audited repository checkout")
    p.add_argument("repo")
    sub.add_parser("repo-list", help="list trusted repository checkouts")
    p = sub.add_parser("repo-remove", help="remove one unused repository checkout")
    p.add_argument("repo")
    p = sub.add_parser("link", help="link one or more repository KUHBs")
    p.add_argument("source", nargs="+")
    p = sub.add_parser("unlink", help="unlink one or more absent repository KUHBs")
    p.add_argument("source", nargs="+")
    sub.add_parser("check", help="validate defaults and active KUHB definitions")
    sub.add_parser("list", help="list local KUHB status")
    sub.add_parser("gui", help="start the KUHBS GUI")
    sub.add_parser("help", help="show this help")
    return parser

def make_context(args: argparse.Namespace, defaults: dict) -> OperationContext:
    # Every command gets fresh terminal/runner state while startup validation owns defaults loading
    logger = EventLogger(source_width=defaults["logging"]["spacing"])
    return OperationContext(defaults=defaults, logger=logger)


def _print_broken(validated) -> None:
    # Check/list/all commands report the complete structured snapshot without raising away valid siblings
    for broken in getattr(validated, "broken_kuhbs", ()):
        print(f"BROKEN {broken.active_id}")
        for issue in broken.issues:
            print(f"  {issue.path}: {issue.message}")


def _plan_runs(plan, *, allow_skips: bool) -> bool:
    # Explicit requests are strict; only an -all expansion may run the possible subset
    print_plan(plan, show_runnable=allow_skips or not plan.skipped)
    return not plan.is_empty() and (allow_skips or plan.can_run_exact)


def _run_definition_plan(ctx: OperationContext, plan, run_selected, *, allow_skips: bool) -> int:
    if not _plan_runs(plan, allow_skips=allow_skips):
        return 1
    run_selected(plan.runnable)
    return 0


def _planned_archive_targets(ctx: OperationContext, definitions: tuple[dict, ...], include_dom0: bool) -> list[archive_targets.ArchiveTarget]:
    # Convert approved KUHB definitions into concrete archive workers after the state-only plan
    return archive_targets.targets_for_all(ctx, definitions, backup.backup_enabled_kuhs, include_dom0=include_dom0)


def _execute_archive_plan(ctx: OperationContext, plan, run_selected, *, allow_skips: bool) -> int:
    # Parent definitions expand only after the shared request admission has succeeded
    if not _plan_runs(plan, allow_skips=allow_skips):
        return 1
    selected = tuple(definition for definition in plan.runnable if definition["id"] != "dom0")
    # Dom0 has no KUHB definition, so archive expansion receives it through its dedicated flag
    include_dom0 = any(definition["id"] == "dom0" for definition in plan.runnable)
    run_selected(_planned_archive_targets(ctx, selected, include_dom0))
    return 0

def _run_main(argv: list[str] | None = None) -> int:
    # Parse syntax, validate every shared input, then route the selected command
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    try:
        args = parser.parse_args(normalize_global_options(argv))
    except SystemExit as exc:
        # Tests and GUI callers invoke main directly, so preserve argparse's exact exit status
        return int(exc.code)
    if args.command == "gui":
        # Replace this process' short CLI marker with the GUI marker owned by gui.main
        CLI_MARKER.unlink(missing_ok=True)
        from .gui import main as gui_main
        gui_args = ["--defaults", str(args.defaults)] if args.defaults else []
        return gui_main(gui_args)
    try:
        validated = inspect_startup_config(
            args.defaults if args.defaults else None,
            check_qubes=args.command == "check",
        )
    except ConfigValidationError as exc:
        print(str(exc))
        return 1
    if args.command is None or args.command == "help":
        parser.print_help()
        return 0
    if args.command == "check":
        if getattr(validated, "broken_kuhbs", ()):
            _print_broken(validated)
            return 1
        print("Configuration validated")
        return 0
    ctx = make_context(args, validated.defaults)
    try:
        if args.command == "list":
            # Status probes receive valid definitions only; broken links are reported after the usable table
            print_list(ctx, validated.kuhb_definitions)
            if getattr(validated, "broken_kuhbs", ()):
                _print_broken(validated)
                return 1
            return 0
        if args.command == "repo-add":
            repository.add_repo(ctx, args.url, branch=args.branch)
            return 0
        if args.command == "update-repo":
            repository.update_repo(ctx, args.repo)
            return 0
        if args.command == "repo-list":
            repository.list_repos(ctx)
            return 0
        if args.command == "repo-remove":
            repository.remove_repo(ctx, args.repo)
            return 0
        if args.command == "link":
            repository.link_kuhbs(ctx, args.source)
            return 0
        if args.command == "unlink":
            repository.unlink_kuhbs(ctx, args.source)
            return 0
        if args.command == "terminal":
            terminal.run(
                ctx,
                args.kuh,
                user=args.user,
                dispvm=args.dispvm,
                shutdown_on_exit_0=args.shutdown_on_exit_0,
            )
            return 0
        definitions = tuple(validated.kuhb_definitions)
        broken = getattr(validated, "broken_kuhbs", ())
        if args.command in {"create", "create-all", "remove", "remove-all"}:
            # -all only chooses the possible subset; explicit requests remain all-or-nothing
            all_targets = args.command.endswith("-all")
            action = args.command.removesuffix("-all")
            if all_targets:
                plan = build_all_action_plan(ctx, definitions, broken, args.command)
            else:
                plan = build_action_plan(
                    ctx,
                    definitions,
                    args.kuhb,
                    action,
                    ordered=True,
                    reverse=action == "remove",
                    broken_reasons=broken_reasons(broken),
                )
            run_selected = create.run_all if action == "create" else remove.run_all
            return _run_definition_plan(
                ctx,
                plan,
                lambda runnable: run_selected(ctx, runnable),
                allow_skips=all_targets,
            )
        if args.command in {"upgrade", "upgrade-all"}:
            # Dom0 and base TemplateVMs use the same shared target admission as GUI Upgrade
            all_targets = args.command == "upgrade-all"
            plan = (
                build_all_action_plan(ctx, definitions, broken, args.command)
                if all_targets
                else build_action_plan(
                    ctx,
                    definitions,
                    args.kuhb,
                    "upgrade",
                    broken_reasons=broken_reasons(broken),
                )
            )
            if not _plan_runs(plan, allow_skips=all_targets):
                return 1
            upgrade_dom0 = any(definition["id"] == "dom0" for definition in plan.runnable)
            selected = [definition for definition in plan.runnable if definition["id"] != "dom0"]
            upgrade_batch.run_targets(
                ctx,
                selected,
                upgrade_dom0=upgrade_dom0,
                qubes_templates=plan.qubes_templates,
            )
            return 0
        if args.command in {"backup", "backup-all", "restore", "restore-all"}:
            # Archive participation and lifecycle admission are shared with GUI sensitivity
            all_targets = args.command.endswith("-all")
            action = args.command.removesuffix("-all")
            plan = (
                build_all_action_plan(ctx, definitions, broken, args.command)
                if all_targets
                else build_action_plan(
                    ctx,
                    definitions,
                    args.target,
                    action,
                    broken_reasons=broken_reasons(broken),
                )
            )
            operation = backup.run_archive_targets if action == "backup" else restore.run_archive_targets
            run_selected = (
                (lambda targets: operation(ctx, targets, source=args.command))
                if all_targets
                else (lambda targets: operation(ctx, targets))
            )
            return _execute_archive_plan(ctx, plan, run_selected, allow_skips=all_targets)
        if args.command == "backup-mount":
            # backup-mount owns user interaction for headerless encrypted backup storage
            backup_mount.run_mount(ctx)
            return 0
        if args.command == "backup-umount":
            # backup-umount intentionally mirrors umount so busy mount errors stay familiar
            backup_mount.run_unmount(ctx)
            return 0
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        # End users need the failing command/reason, not a Python traceback
        ctx.logger.error("kuhbs", str(exc))
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    # One plain marker serializes every CLI invocation before config or Qubes work starts
    if CLI_MARKER.exists():
        print("Another KUHBS CLI is already running")
        return 1
    if GUI_MARKER.exists() and os.environ.get("KUHBS_FROM_GUI") != "1":
        print("Close the KUHBS GUI before running the CLI manually")
        return 1
    CLI_MARKER.parent.mkdir(parents=True, exist_ok=True)
    CLI_MARKER.touch()
    try:
        return _run_main(argv)
    finally:
        CLI_MARKER.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
