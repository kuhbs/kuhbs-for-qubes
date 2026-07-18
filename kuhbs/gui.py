# Purpose: GTK GUI for local kuhb discovery and operations
# Scope: Visual styling is loaded from install templates instead of embedded CSS
from __future__ import annotations

import argparse
from datetime import datetime
from html import escape
from pathlib import Path
from shlex import quote
import subprocess
import sys
import threading

from .config import repo_defaults_path, resolve_path
from .validation import (
    BrokenKuhb,
    ConfigValidationError,
    _load_schema_validators,
    inspect_startup_config,
    validate_definition_set,
    validate_kuhb_file,
)
from .log import EventLogger
from .listing import collect_status, _backup_label as list_backup_label
from .model import action_allowed, base_template_vm_names, display_state, resolve_kuhs
from .operations import OperationContext
from .operations import archive_storage as archive_storage_op
from .operations.planning import broken_reasons, build_action_plan, build_all_action_plan
from .operations import repository
from .operations import upgrade as upgrade_op
from .terminal import dom0_terminal_command_prefix

# GUI actions intentionally mirror CLI operation names
ACTIONS = ("Create", "Upgrade", "Backup", "Restore", "Remove")
CLI_MARKER = Path.home() / ".kuhbs/states/cli"
GUI_MARKER = Path.home() / ".kuhbs/states/gui"


def _css_path() -> Path:
    # Installed systems use /usr/share; source-tree launches use the template copy
    installed = Path("/usr/share/kuhbs/kuhbs.css")
    if installed.exists():
        return installed
    source_tree = Path(__file__).resolve().parents[1] / "install/templates/usr/share/kuhbs/kuhbs.css"
    if source_tree.exists():
        return source_tree
    raise FileNotFoundError(f"KUHBS CSS not found: {installed}")


def _style(widget, *classes: str) -> None:
    # Apply CSS classes from the external stylesheet; keep layout code free of visual constants
    context = widget.get_style_context()
    for class_name in classes:
        context.add_class(class_name)


def _kuhb_sort_key(definition: dict) -> tuple[int, str]:
    # Sort cards by configured order first, then by display name for stable GUI lists
    order = definition.get("order", 9999)
    if not isinstance(order, int):
        order = 9999
    return (order, str(definition.get("name", definition["id"])).lower())


def _broken_card_definition(broken: BrokenKuhb) -> dict:
    # Preserve safe display metadata, but the active directory name always owns card identity
    definition = dict(broken.definition or {})
    order = definition.get("order")
    if not isinstance(order, int) or isinstance(order, bool) or not 1 <= order <= 1000:
        order = 1000
    definition.update(
        {
            "id": broken.active_id,
            "name": definition.get("name") or broken.active_id,
            "description": "\n".join(f"{issue.path}: {issue.message}" for issue in broken.issues),
            "order": order,
            "type": definition.get("type", "unknown"),
            "_broken": True,
            "_path": broken.path,
        }
    )
    return definition


def _broken_fingerprint(broken_kuhbs) -> tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...]:
    # Stable content, not refresh time or object identity, controls one-shot error alerts
    return tuple(
        (
            broken.active_id,
            str(broken.path),
            tuple((str(issue.path), issue.message) for issue in broken.issues),
        )
        for broken in broken_kuhbs
    )


def _new_broken_entries(broken_kuhbs, shown: set) -> list[BrokenKuhb]:
    # Keep unchanged errors dismissed while clearing fixed errors so recurrence alerts again
    fingerprints = _broken_fingerprint(broken_kuhbs)
    current = set(fingerprints)
    shown.intersection_update(current)
    new = current - shown
    shown.update(new)
    changed: list[BrokenKuhb] = []
    pending = set(new)
    for broken, fingerprint in zip(broken_kuhbs, fingerprints):
        if fingerprint not in pending:
            continue
        changed.append(broken)
        pending.remove(fingerprint)
    return changed


def _show_copyable_error(Gtk, title: str, text: str, *, parent=None, ok_only: bool = False) -> None:
    # Every graphical error is copyable and mirrored to stderr before the modal opens
    print(f"{title}: {text}", file=sys.stderr, flush=True)
    dialog = Gtk.Dialog(title=title, transient_for=parent, flags=Gtk.DialogFlags.MODAL)
    if ok_only:
        dialog.add_button("OK", Gtk.ResponseType.OK)
    else:
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
    dialog.set_default_size(900, 520)
    box = dialog.get_content_area()
    label = Gtk.Label(label=title, xalign=0)
    label.set_margin_top(12)
    label.set_margin_bottom(8)
    label.set_margin_start(12)
    label.set_margin_end(12)
    box.pack_start(label, False, False, 0)
    scrolled = Gtk.ScrolledWindow()
    scrolled.set_margin_start(12)
    scrolled.set_margin_end(12)
    scrolled.set_margin_bottom(12)
    scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    view = Gtk.TextView()
    view.set_editable(False)
    view.set_cursor_visible(True)
    view.set_monospace(True)
    view.set_wrap_mode(Gtk.WrapMode.NONE)
    view.get_buffer().set_text(text)
    scrolled.add(view)
    box.pack_start(scrolled, True, True, 0)
    dialog.show_all()
    dialog.run()
    dialog.destroy()


def _show_validation_notice(Gtk, parent):
    # Reuse the standard confirmation-dialog surface so every Qubes theme paints an opaque text box
    dialog = Gtk.MessageDialog(
        transient_for=parent,
        flags=Gtk.DialogFlags.MODAL,
        message_type=Gtk.MessageType.INFO,
        buttons=Gtk.ButtonsType.NONE,
        text="Validating configuration",
    )
    dialog.format_secondary_text("All actions are blocked. Please wait...")
    dialog.set_deletable(False)
    # The delete handler blocks both window-manager close requests and Gtk.Dialog's Escape action
    dialog.connect("delete-event", lambda *_args: True)
    dialog.set_resizable(False)
    return dialog


def _validate_gui_config(Gtk, GLib, defaults_path: Path, *, parent=None):
    # Keep GTK painting the standard modal while complete file and Qubes XML validation runs off the UI thread
    dialog = _show_validation_notice(Gtk, parent)
    outcome = {}

    def validate():
        try:
            validated = inspect_startup_config(defaults_path, check_qubes=True)
            hard_issues = [
                issue
                for broken in validated.broken_kuhbs
                for issue in broken.issues
                if issue.path != broken.path
            ]
            if hard_issues:
                raise ConfigValidationError(hard_issues)
            outcome["validated"] = validated
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            GLib.idle_add(dialog.response, Gtk.ResponseType.OK)

    worker = threading.Thread(target=validate, daemon=True)
    worker_started = False

    def start_validation():
        # Starting from the dialog's nested GTK loop guarantees the standard popup is active first
        nonlocal worker_started
        try:
            worker.start()
        except BaseException as exc:
            # A no-button modal must always receive a response even when no worker can be created
            outcome["error"] = exc
            dialog.response(Gtk.ResponseType.OK)
        else:
            worker_started = True
        return False

    GLib.idle_add(start_validation)
    dialog.run()
    if worker_started:
        worker.join()

    validation_error = outcome.get("error")
    if validation_error is None:
        # The caller keeps the already-painted modal open through its status refresh
        return outcome["validated"], dialog
    dialog.destroy()
    _show_copyable_error(
        Gtk,
        "KUHBS validation failed",
        str(validation_error),
        parent=parent,
    )
    if parent is not None:
        parent.destroy()
    return None


def _validate_live_gui_config(Gtk, GLib, defaults_path: Path, parent):
    # Exceptions escaping a GLib callback are only logged, so fail closed instead of leaving stale controls blocked
    try:
        return _validate_gui_config(Gtk, GLib, defaults_path, parent=parent)
    except BaseException as exc:
        _show_copyable_error(
            Gtk,
            "KUHBS validation failed",
            str(exc),
            parent=parent,
        )
        parent.destroy()
        return None


def _run_gui(argv: list[str] | None = None) -> int:
    # Import GTK lazily so CLI commands and tests do not require PyGObject
    # Validate config, route the selected command, and return a process exit code
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk, Gdk, GdkPixbuf, Gio, GLib, Pango
    except Exception as exc:
        raise SystemExit(f"GTK 3/PyGObject is required for kuhbs-gui: {exc}")

    parser = argparse.ArgumentParser(prog="kuhbs-gui")
    parser.add_argument("--defaults", type=Path, default=None)
    args = parser.parse_args(argv)
    defaults_path = args.defaults if args.defaults else repo_defaults_path()

    provider = Gtk.CssProvider()
    # Build and style the complete static interface before validated data is available
    provider.load_from_path(str(_css_path()))
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    settings = Gtk.Settings.get_default()
    settings.set_property("gtk-tooltip-timeout", 1000)
    settings.set_property("gtk-tooltip-browse-timeout", 1000)

    window = Gtk.Window(title="KUHBS GUI")
    _style(window, "kuhbs-window")
    window.set_default_size(900, 700)
    window.set_deletable(False)

    notebook = Gtk.Notebook()
    _style(notebook, "kuhbs-notebook")
    window.add(notebook)

    def on_window_key_press(_window, event):
        # Handle global keyboard shortcuts before focused widgets consume them
        if event.keyval == Gdk.KEY_Escape:
            clear_selection()
            return True
        if event.state & Gdk.ModifierType.CONTROL_MASK and event.keyval in {Gdk.KEY_a, Gdk.KEY_A}:
            # Focused text fields own Ctrl+A; global card selection applies only elsewhere.
            if isinstance(window.get_focus(), Gtk.Editable):
                return False
            select_all_visible()
            return True
        if event.state & Gdk.ModifierType.MOD1_MASK:
            pages = {Gdk.KEY_1: 0, Gdk.KEY_2: 1, Gdk.KEY_3: 2, Gdk.KEY_4: 3}
            if event.keyval in pages:
                notebook.set_current_page(pages[event.keyval])
                return True
        return False

    window.connect("key-press-event", on_window_key_press)

    def toolbar_button(label: str, *styles: str):
        # Create a consistently styled fixed-width toolbar button
        button = Gtk.Button(label=label)
        _style(button, "kuhbs-button", *styles)
        button.set_size_request(110, -1)
        button.set_hexpand(False)
        return button

    def toolbar_row(left_toolbar, search, right_toolbar):
        # Flow each control independently so narrow windows gain rows instead of hiding buttons.
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_column_spacing(6)
        flow.set_row_spacing(6)
        flow.set_min_children_per_line(1)
        flow.set_max_children_per_line(50)
        flow.set_hexpand(True)
        left_children = left_toolbar.get_children()
        right_children = right_toolbar.get_children()
        for child in left_children:
            left_toolbar.remove(child)
        for child in right_children:
            right_toolbar.remove(child)
        search.set_halign(Gtk.Align.CENTER)
        search.set_valign(Gtk.Align.CENTER)
        for child in [*left_children, search, *right_children]:
            flow.add(child)
        return flow

    repos_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    _style(repos_box, "kuhbs-page")
    app_search = Gtk.SearchEntry()
    _style(app_search, "kuhbs-search")
    app_search.set_placeholder_text("Search repos")
    app_search.set_width_chars(25)
    app_search.set_max_width_chars(25)
    repo_left_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    repo_right_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    install_button = toolbar_button("Add repo", "kuhbs-primary")
    update_button = toolbar_button("Update repo")
    update_button.set_sensitive(False)
    repo_edit_button = toolbar_button("Edit")
    repo_edit_button.set_sensitive(False)
    repo_link_button = toolbar_button("Enable")
    repo_link_button.set_sensitive(False)
    repo_unlink_button = toolbar_button("Disable", "kuhbs-danger")
    repo_unlink_button.set_sensitive(False)
    repo_left_toolbar.pack_start(install_button, False, False, 0)
    repo_left_toolbar.pack_start(update_button, False, False, 0)
    repo_left_toolbar.pack_start(repo_edit_button, False, False, 0)
    repo_right_toolbar.pack_start(repo_link_button, False, False, 0)
    repo_right_toolbar.pack_start(repo_unlink_button, False, False, 0)
    repo_toolbar = toolbar_row(repo_left_toolbar, app_search, repo_right_toolbar)
    repos_box.pack_start(repo_toolbar, False, False, 0)
    app_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    app_scrolled = Gtk.ScrolledWindow()
    app_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    app_scrolled.add(app_list)
    repos_box.pack_start(app_scrolled, True, True, 8)


    def make_kuhbs_page(
        search_placeholder: str,
        *,
        include_global_buttons: bool,
        actions=ACTIONS,
        include_edit: bool = True,
        global_actions: tuple[str, ...] | None = None,
    ):
        # Build aligned card pages while letting Qubes OS targets expose their smaller action set
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        _style(page, "kuhbs-page")
        search = Gtk.SearchEntry()
        _style(search, "kuhbs-search")
        search.set_placeholder_text(search_placeholder)
        search.set_width_chars(25)
        search.set_max_width_chars(25)
        left_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        edit_button = toolbar_button("Edit") if include_edit else None
        if edit_button is not None:
            edit_button.set_sensitive(False)
            left_toolbar.pack_start(edit_button, False, False, 0)
        page_buttons = {action.lower(): toolbar_button(action) for action in actions}
        page_all_buttons = {}
        if include_global_buttons:
            # Remove-All stays CLI-only because the GUI requires explicit selected Remove confirmation
            available_all_buttons = {
                "create-all": toolbar_button("Create all"),
                "upgrade-all": toolbar_button("Upgrade all"),
                "backup-mount": toolbar_button("Backup mount"),
                "backup-umount": toolbar_button("Backup umount"),
                "backup-all": toolbar_button("Backup all"),
                "restore-all": toolbar_button("Restore all"),
            }
            selected_global_actions = global_actions or tuple(available_all_buttons)
            page_all_buttons = {action: available_all_buttons[action] for action in selected_global_actions}
        for action, button in page_buttons.items():
            styles = ["kuhbs-button"]
            if action == "create":
                styles.append("kuhbs-primary")
            if action == "remove":
                styles.append("kuhbs-danger")
            _style(button, *styles)
            left_toolbar.pack_start(button, False, False, 0)
        right_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        if include_global_buttons:
            for action, button in page_all_buttons.items():
                _style(button, "kuhbs-button")
                right_toolbar.pack_start(button, False, False, 0)
        page_toolbar = toolbar_row(left_toolbar, search, right_toolbar)
        page.pack_start(page_toolbar, False, False, 0)
        page_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(page_list)
        page.pack_start(scrolled, True, True, 8)
        return page, search, page_list, scrolled, edit_button, page_buttons, page_all_buttons

    my_box, my_search, my_list, my_scrolled, my_edit_button, buttons, all_buttons = make_kuhbs_page("Search local kuhbs", include_global_buttons=True)
    system_box, system_search, system_list, system_scrolled, system_edit_button, system_buttons, system_all_buttons = make_kuhbs_page("Search system kuhbs", include_global_buttons=True)
    qubes_os_box, qubes_os_search, qubes_os_list, qubes_os_scrolled, _qubes_os_edit, qubes_os_buttons, qubes_os_all_buttons = make_kuhbs_page(
        "Search Qubes OS VMs",
        include_global_buttons=True,
        actions=("Upgrade", "Backup", "Restore"),
        include_edit=False,
        global_actions=("upgrade-all",),
    )
    notebook.append_page(my_box, Gtk.Label(label="My KUHBS"))
    notebook.append_page(system_box, Gtk.Label(label="System KUHBS"))
    notebook.append_page(qubes_os_box, Gtk.Label(label="Qubes OS VMs"))
    notebook.append_page(repos_box, Gtk.Label(label="Repos"))
    notebook.child_set_property(my_box, "tab-expand", True)
    notebook.child_set_property(system_box, "tab-expand", True)
    notebook.child_set_property(qubes_os_box, "tab-expand", True)
    notebook.child_set_property(repos_box, "tab-expand", True)

    install_button.set_tooltip_text("Add a repo URL")
    update_button.set_tooltip_text("Update the selected repo")
    repo_edit_button.set_tooltip_text("Edit all files in the selected repo KUHB")
    repo_link_button.set_tooltip_text("Enable the selected repo KUHB in My KUHBS")
    repo_unlink_button.set_tooltip_text("Disable the selected absent repo KUHB from My KUHBS")
    my_edit_button.set_tooltip_text("Edit the selected local KUHB YAML")
    system_edit_button.set_tooltip_text("Edit the selected system KUHB YAML")
    for button_map in (buttons, system_buttons):
        button_map["create"].set_tooltip_text("Create the selected KUHBs")
        button_map["upgrade"].set_tooltip_text("Upgrade the selected KUHBs")
        button_map["backup"].set_tooltip_text("Back up the selected KUHBs")
        button_map["restore"].set_tooltip_text("Restore the selected KUHBs")
        button_map["remove"].set_tooltip_text("Remove the selected KUHBs")
    qubes_os_buttons["upgrade"].set_tooltip_text("Upgrade the selected Qubes OS VMs")
    qubes_os_buttons["backup"].set_tooltip_text("Back up dom0")
    qubes_os_buttons["restore"].set_tooltip_text("Restore dom0")
    for button_map in (all_buttons, system_all_buttons):
        button_map["create-all"].set_tooltip_text("Run kuhbs create-all")
        button_map["upgrade-all"].set_tooltip_text("Run kuhbs upgrade-all")
        button_map["backup-mount"].set_tooltip_text("Unlock and mount KUHBS backup storage")
        button_map["backup-umount"].set_tooltip_text("Unmount and lock KUHBS backup storage")
        button_map["backup-all"].set_tooltip_text("Run kuhbs backup-all")
        button_map["restore-all"].set_tooltip_text("Run kuhbs restore-all")
    qubes_os_all_buttons["upgrade-all"].set_tooltip_text("Run kuhbs upgrade-all")

    # Show the real tabs and toolbars first, but keep every control blocked until validation succeeds
    notebook.set_sensitive(False)
    window.show_all()
    validation_result = _validate_gui_config(Gtk, GLib, defaults_path, parent=window)
    if validation_result is None:
        return 1
    validated, startup_validation_dialog = validation_result
    window.connect("destroy", Gtk.main_quit)
    defaults = validated.defaults
    _defaults_validator, repo_raw_validator, repo_resolved_validator = _load_schema_validators()
    ctx = OperationContext(defaults=defaults, logger=EventLogger(stdout=False))
    status_by_kuhb = {}
    dom0_state_cache = "dom0"
    # The passive running cache prevents insensitive buttons from probing or starting the backup VM
    backup_vm_running_cache = False
    backup_mounted_cache = False
    backup_cleanup_available_cache = False
    status_refresh_running = False
    validation_running = False
    status_refresh_requested = False
    status_refresh_pending = False

    selected: set[str] = set()
    visible_kuhb_ids: list[str] = []
    selection_anchor: str | None = None
    system_selected: set[str] = set()
    visible_system_kuhb_ids: list[str] = []
    system_selection_anchor: str | None = None
    qubes_os_selected: set[str] = set()
    visible_qubes_os_vm_ids: list[str] = []
    qubes_os_selection_anchor: str | None = None
    repo_selected: set[str] = set()
    visible_repo_sources: list[str] = []
    repo_linkable_sources: set[str] = set()
    repo_unlinkable_sources: set[str] = set()
    repo_source_kuhbs: dict[str, str] = {}
    repo_selection_anchor: str | None = None
    selected_repos: set[str] = set()
    # Selection lives in memory only; persistent state remains the plain state files
    running_ops: list[dict] = []
    editor_processes: list[subprocess.Popen] = []
    busy_fields: dict[str, set[str]] = {}
    local_definition_cache: list[dict] = list(validated.kuhb_definitions)
    local_broken_cache: list[BrokenKuhb] = list(validated.broken_kuhbs)
    qubes_vm_cache = dict(getattr(validated, "qubes_vms", {}))
    repo_broken_cache: list[BrokenKuhb] = []
    shown_broken_fingerprints: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()

    def alert_broken_changes(broken_kuhbs) -> None:
        # Alert only new or changed entries; removing one clears its latch so recurrence alerts again
        changed = _new_broken_entries(broken_kuhbs, shown_broken_fingerprints)
        if not changed:
            return
        instructions = "\n".join(f"Edit the broken KUHB {broken.active_id} and try again." for broken in changed)
        text = instructions + "\n\n" + "\n\n".join(str(broken.error) for broken in changed)
        _show_copyable_error(
            Gtk,
            "Broken KUHB configuration",
            text,
            parent=window,
            ok_only=True,
        )

    def mark_busy(kuhb_ids, field: str | None) -> None:
        # Track a running terminal operation so buttons and spinners stay disabled
        if field is None:
            return
        for kuhb_id in kuhb_ids:
            busy_fields.setdefault(kuhb_id, set()).add(field)

    def unmark_busy(kuhb_ids, field: str | None) -> None:
        # Remove a finished terminal operation and refresh button state
        if field is None:
            return
        for kuhb_id in kuhb_ids:
            fields = busy_fields.get(kuhb_id)
            if not fields:
                continue
            fields.discard(field)
            if not fields:
                busy_fields.pop(kuhb_id, None)

    def busy(kuhb_id: str, field: str) -> bool:
        # Check whether one card currently owns a running operation
        return field in busy_fields.get(kuhb_id, set())

    def any_busy(kuhb_ids) -> bool:
        # Check global busy state before starting actions that should not overlap
        return any(kuhb_id in busy_fields for kuhb_id in kuhb_ids)

    def actions_blocked() -> bool:
        # Post-operation refresh keeps stale buttons and callbacks blocked until fresh status replaces the cache
        return bool(running_ops) or bool(busy_fields) or status_refresh_running or status_refresh_pending

    def repair_mode() -> bool:
        # Any broken local or repository KUHB freezes mutations until its YAML has been repaired
        return bool(local_broken_cache) or bool(repo_broken_cache)

    def edit_allowed(selected_set: set[str]) -> bool:
        # Normal mode edits any local card; repair mode exposes Edit only for the selected broken KUHB
        if len(selected_set) != 1 or "dom0" in selected_set or any_busy(selected_set) or actions_blocked():
            return False
        if not repair_mode():
            return True
        selected_id = next(iter(selected_set))
        return any(broken.active_id == selected_id for broken in local_broken_cache)

    def repo_edit_allowed() -> bool:
        # Repository repair uses the exact broken YAML path so same-id candidates cannot enable one another
        if len(repo_selected) != 1 or actions_blocked():
            return False
        if not repair_mode():
            return True
        source = next(iter(repo_selected))
        return any(broken.path == ctx.repos_root / source / "kuhb.yml" for broken in repo_broken_cache)

    def backup_status(status_ctx: OperationContext) -> tuple[bool, bool, bool]:
        # Passive qvm-check keeps every GUI storage action disabled without starting a halted VM
        backup_kuh = status_ctx.defaults["backup"]["kuh"]
        running = archive_storage_op.backup_vm_running(status_ctx, backup_kuh)
        if not running:
            return False, False, False
        ready, mounted = archive_storage_op.backup_storage_status(status_ctx, backup_kuh)
        cleanup_available = mounted or archive_storage_op.backup_mapper_active(
            status_ctx,
            backup_kuh,
            status_ctx.defaults["backup"]["crypt_name"],
        )
        return True, ready, cleanup_available

    def refresh_status_cache() -> None:
        # Initial startup already owns one validated definition and defaults snapshot
        nonlocal status_by_kuhb, dom0_state_cache, backup_vm_running_cache, backup_mounted_cache, backup_cleanup_available_cache
        status_by_kuhb = {status.kuhb_id: status for status in collect_status(ctx, local_definition_cache)}
        dom0_state_cache = ctx.state_store.current_gate("dom0")
        # Mount probing is slow, so typing in search must reuse this cached value
        backup_vm_running_cache, backup_mounted_cache, backup_cleanup_available_cache = backup_status(ctx)

    def schedule_status_refresh() -> None:
        # Validate first, collect against that immutable snapshot, then publish definitions and status together
        nonlocal status_by_kuhb, dom0_state_cache, backup_vm_running_cache, backup_mounted_cache, backup_cleanup_available_cache
        nonlocal status_refresh_running, validation_running, status_refresh_requested, status_refresh_pending
        nonlocal local_definition_cache, local_broken_cache, qubes_vm_cache
        if status_refresh_running or validation_running:
            status_refresh_requested = True
            return
        validation_running = True
        try:
            validation_result = _validate_live_gui_config(Gtk, GLib, defaults_path, window)
        finally:
            validation_running = False
        if validation_result is None:
            return
        refreshed, validation_dialog = validation_result
        refresh_definitions = list(refreshed.kuhb_definitions)
        refresh_ctx = OperationContext(defaults=refreshed.defaults, logger=ctx.logger)
        status_refresh_running = True

        def worker() -> None:
            # Run slow status collection outside the GTK thread and hand results back with GLib
            error = None
            new_status_by_kuhb = status_by_kuhb
            new_dom0_state = dom0_state_cache
            new_backup_vm_running = backup_vm_running_cache
            new_backup_mounted = backup_mounted_cache
            new_backup_cleanup_available = backup_cleanup_available_cache
            try:
                new_status_by_kuhb = {
                    status.kuhb_id: status
                    for status in collect_status(refresh_ctx, refresh_definitions)
                }
                new_dom0_state = refresh_ctx.state_store.current_gate("dom0")
                new_backup_vm_running, new_backup_mounted, new_backup_cleanup_available = backup_status(refresh_ctx)
            except Exception as exc:
                error = exc

            def apply() -> bool:
                # Apply one complete validated snapshot on the GTK thread before allowing any repaint
                nonlocal status_by_kuhb, dom0_state_cache, backup_vm_running_cache, backup_mounted_cache, backup_cleanup_available_cache
                nonlocal status_refresh_running, status_refresh_requested, status_refresh_pending
                nonlocal local_definition_cache, local_broken_cache, qubes_vm_cache
                if error is not None:
                    validation_dialog.destroy()
                    _show_copyable_error(Gtk, "KUHBS status refresh failed", str(error))
                    status_refresh_running = False
                    status_refresh_requested = False
                    schedule_status_refresh()
                    return False
                ctx.defaults = refreshed.defaults
                local_definition_cache = refresh_definitions
                local_broken_cache = list(refreshed.broken_kuhbs)
                qubes_vm_cache = dict(getattr(refreshed, "qubes_vms", {}))
                status_by_kuhb = new_status_by_kuhb
                dom0_state_cache = new_dom0_state
                backup_vm_running_cache = new_backup_vm_running
                backup_mounted_cache = new_backup_mounted
                backup_cleanup_available_cache = new_backup_cleanup_available
                status_refresh_running = False
                if status_refresh_requested:
                    validation_dialog.destroy()
                    status_refresh_requested = False
                    schedule_status_refresh()
                    return False
                status_refresh_pending = False
                refresh_my()
                refresh_system()
                refresh_qubes_os()
                refresh_app_store()
                validation_dialog.destroy()
                return False

            GLib.idle_add(apply)

        threading.Thread(target=worker, daemon=True).start()

    def gui_state(kuhb_id: str) -> str:
        # Cards and action plans read only the lifecycle snapshot published by the last refresh
        if kuhb_id == "dom0":
            return dom0_state_cache
        status = status_by_kuhb.get(kuhb_id)
        return status.state if status is not None else "linked"

    def gui_action_plan(action: str, requested_ids=()):
        # GUI repaints pass cached gates into the same side-effect-free planner used by the CLI
        states = {"dom0": dom0_state_cache, **{key: value.state for key, value in status_by_kuhb.items()}}
        definitions = tuple(local_definition_cache)
        if action.endswith("-all"):
            # All expands every configured candidate and keeps blocked reasons for terminal output
            return build_all_action_plan(ctx, definitions, tuple(local_broken_cache), action, states=states)
        return build_action_plan(
            ctx, definitions, list(requested_ids), action,
            broken_reasons=broken_reasons(local_broken_cache), states=states,
        )

    def refresh_buttons(button_map: dict, all_button_map: dict, cards: list[dict]) -> None:
        # Multi-select actions are enabled only when every selected kuhb allows them
        mounted = backup_mounted_cache
        # A halted autostart backup VM disables Mount and Umount instead of being started implicitly
        backup_vm_running = backup_vm_running_cache
        cleanup_available = backup_cleanup_available_cache
        running = actions_blocked() or repair_mode()
        selected_ids = [card["id"] for card in cards if card.get("selected")]
        selected_busy = any_busy(selected_ids)
        for action, button in button_map.items():
            plan = gui_action_plan(action, selected_ids)
            storage_ready = action not in {"backup", "restore"} or mounted
            sensitive = bool(selected_ids) and not running and not selected_busy and storage_ready and plan.can_run_exact
            button.set_sensitive(sensitive)
        if all_button_map:
            # Every All button is enabled when its shared expansion finds at least one possible target
            for action in ("create-all", "upgrade-all", "backup-all", "restore-all"):
                if action in all_button_map:
                    plan = gui_action_plan(action)
                    storage_ready = action not in {"backup-all", "restore-all"} or mounted
                    all_button_map[action].set_sensitive(not running and storage_ready and not plan.is_empty())
            # A partial mount or stale mapper must be cleaned up before the interactive mount helper
            if "backup-mount" in all_button_map:
                all_button_map["backup-mount"].set_sensitive(not running and backup_vm_running and not cleanup_available)
            if "backup-umount" in all_button_map:
                all_button_map["backup-umount"].set_sensitive(not running and backup_vm_running and cleanup_available)

    def _kuhb_icon(definition: dict, root: Path):
        # Broken or missing icons should not prevent the GUI from showing actionable cards
        icon_name = definition.get("icon")
        icon_path = root / icon_name if isinstance(icon_name, str) and icon_name else None
        image = Gtk.Image()
        image.set_size_request(42, 42)
        image.set_halign(Gtk.Align.CENTER)
        image.set_valign(Gtk.Align.CENTER)
        if definition.get("_broken"):
            image.set_from_icon_name("application-x-executable", Gtk.IconSize.DND)
            return _icon_cell(image)
        try:
            if icon_path is None:
                raise FileNotFoundError("kuhb icon missing")
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(icon_path), 42, 42, True)
            image.set_from_pixbuf(pixbuf)
        except Exception:
            image.set_from_icon_name("application-x-executable", Gtk.IconSize.DND)
        return _icon_cell(image)

    def _age_label(timestamp) -> str:
        # Render a timestamp age compactly for status badges
        if timestamp is None:
            return "Never"
        seconds = max(0, int((datetime.now().astimezone() - timestamp).total_seconds()))
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m"
        return f"{minutes // 60}h"

    def _upgrade_age(definition: dict) -> str:
        # Extract the age part from upgraded/upgradable status strings
        timestamps = [
            upgrade_op.last_kuh_upgrade(ctx, kuh.name)
            for kuh in resolve_kuhs(definition)
            if upgrade_op.kuh_has_upgrade_work(ctx, definition, kuh)
        ]
        if not timestamps:
            return "Never"
        if any(timestamp is None for timestamp in timestamps):
            return "Never"
        return _age_label(min(timestamp for timestamp in timestamps if timestamp is not None))

    def _dom0_health(action: str, completed_color: str) -> tuple[str, str]:
        # Dom0 has no KUHB rows, so its own action files provide compact health.
        state = ctx.state_store.get_state("dom0", action)
        if state is None:
            return "never", "never"
        if state == "start":
            return "Running", "linked"
        if state == "failed":
            return "Failed", "broken"
        path = ctx.state_store.action_path("dom0", action)
        timestamp = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        return _age_label(timestamp), completed_color

    def _one_line(label) -> None:
        # Collapse multi-line descriptions so GTK labels stay compact
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_single_line_mode(True)
        label.set_lines(1)

    def _fixed_cell(widget, width: int):
        # Create fixed-width label cells so header and card columns line up
        cell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        cell.set_size_request(width, -1)
        cell.set_hexpand(False)
        cell.pack_start(widget, True, True, 0)
        return cell

    def _list_color_class(state: str) -> str:
        # Match the color groups used by `kuhbs list`
        if state in {"create:completed", "backup:completed", "restore:completed", "running", "paused", "present", "upgraded", "dom0"}:
            return "kuhbs-list-green"
        if state in {"linked", "create:start", "backup:start", "restore:start", "remove:start"}:
            return "kuhbs-list-orange"
        if state in {"halted", "launcher", "never", "unavailable", "", "remove:completed"}:
            return "kuhbs-list-gray"
        return "kuhbs-list-red"

    def _display_meta(text: str) -> str:
        # Build the left-side title and description text for a card
        return {
            "linked": display_state("linked"),
            "create:start": display_state("create:start"),
            "create:failed": display_state("create:failed"),
            "create:completed": display_state("create:completed"),
            "backup:start": display_state("backup:start"),
            "backup:failed": display_state("backup:failed"),
            "backup:completed": display_state("backup:completed"),
            "restore:start": display_state("restore:start"),
            "restore:failed": display_state("restore:failed"),
            "restore:completed": display_state("restore:completed"),
            "remove:start": display_state("remove:start"),
            "remove:failed": display_state("remove:failed"),
            "remove:completed": display_state("remove:completed"),
            "present": "Present",
            "never": "Never",
            "upgraded": "Upgraded",
            "upgradable": "Upgradable",
        }.get(text, text)

    def _right_meta(text: str, width: int, chars: int, color_state: str = ""):
        # Build the right-side kind/state metadata for a card
        label = Gtk.Label(label=_display_meta(text), xalign=0.5)
        _style(label, "kuhbs-meta-right")
        if color_state:
            _style(label, _list_color_class(color_state))
        label.set_halign(Gtk.Align.CENTER)
        label.set_width_chars(chars)
        label.set_max_width_chars(chars)
        _one_line(label)
        return _fixed_cell(label, width)

    def _right_spinner(width: int):
        # Show a spinner only for columns affected by the running action
        spinner = Gtk.Spinner()
        spinner.set_halign(Gtk.Align.CENTER)
        spinner.set_valign(Gtk.Align.CENTER)
        spinner.set_size_request(24, 24)
        spinner.start()
        return _fixed_cell(spinner, width)

    def _right_header(text: str, width: int, chars: int):
        # Build the right-side status cells for each card row
        label = Gtk.Label(label=text, xalign=0.5)
        _style(label, "kuhbs-column-header")
        label.set_halign(Gtk.Align.CENTER)
        label.set_width_chars(chars)
        label.set_max_width_chars(chars)
        _one_line(label)
        return _fixed_cell(label, width)

    def _icon_cell(child=None):
        # Load the card icon into a fixed-size image cell
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        box.set_size_request(52, 46)
        box.set_margin_end(10)
        box.set_margin_top(2)
        box.set_margin_bottom(2)
        if child is not None:
            box.pack_start(child, True, True, 0)
        return box

    def _status_header_block(show_order: bool = True):
        # Create one labeled status block for backup, upgrade, or lifecycle state
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=34)
        status_box.set_halign(Gtk.Align.END)
        status_box.pack_start(_right_header("State", 116, 12), False, False, 0)
        status_box.pack_start(_right_header("Backup", 104, 11), False, False, 0)
        status_box.pack_start(_right_header("Upgrade", 98, 10), False, False, 0)
        if show_order:
            status_box.pack_start(_right_header("Order", 68, 7), False, False, 0)
        return status_box

    def _empty_status_block():
        # Create an empty fixed-size block when a status column does not apply
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=34)
        status_box.set_halign(Gtk.Align.END)
        status_box.pack_start(_right_header("", 116, 12), False, False, 0)
        status_box.pack_start(_right_header("", 104, 11), False, False, 0)
        status_box.pack_start(_right_header("", 98, 10), False, False, 0)
        status_box.pack_start(_right_header("", 68, 7), False, False, 0)
        return status_box

    def make_header_row(repository: bool = False, show_order: bool = True):
        # Repos own one status cell; KUHB pages keep lifecycle and health columns.
        frame = Gtk.Frame()
        _style(frame, "kuhbs-header-row")
        frame.set_shadow_type(Gtk.ShadowType.NONE)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        row.set_margin_top(6)
        row.set_margin_bottom(6)
        row.set_margin_start(10)
        row.set_margin_end(10)
        row.pack_start(_icon_cell(), False, False, 0)
        row.pack_start(Gtk.Box(), True, True, 0)
        header = _right_header("Status", 116, 12) if repository else _status_header_block(show_order)
        row.pack_start(header, False, False, 0)
        frame.add(row)
        return frame

    def repo_display_url(repo_root: Path, repo_id: str) -> str:
        # Render repo ids as cloneable HTTPS URLs for the Add/Update page
        result = subprocess.run(["git", "-C", str(repo_root), "remote", "get-url", "origin"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        return result.stdout.strip() or repo_id

    def make_repo_header_row(repo_id: str, repo_root: Path, selected_header: bool):
        # Assemble the repo row using the same visual structure as KUHB cards
        frame = Gtk.Frame()
        _style(frame, "kuhbs-repo-title-selected" if selected_header else "kuhbs-repo-title-frame")
        frame.set_shadow_type(Gtk.ShadowType.NONE)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        row.set_margin_top(6)
        row.set_margin_bottom(6)
        row.set_margin_start(10)
        row.set_margin_end(10)
        row.pack_start(_icon_cell(), False, False, 0)
        label = Gtk.Label(label=repo_display_url(repo_root, repo_id), xalign=0)
        _style(label, "kuhbs-repo-title")
        _one_line(label)
        row.pack_start(label, True, True, 0)
        row.pack_start(_empty_status_block(), False, False, 0)
        frame.add(row)
        event = Gtk.EventBox()
        event.add(frame)
        event.repo_id = repo_id
        return event

    def _backup_label(definition: dict) -> tuple[str, str]:
        # Format backup status with the same wording as the CLI list output
        if definition["id"] == "dom0":
            return _dom0_health("backup", "present")
        status = status_by_kuhb.get(definition["id"])
        if status is None:
            return "", ""
        # Backup status is collected only by refresh_status_cache so search typing stays local-only
        backups = [kuh.backup for kuh in status.kuhs if kuh.backup]
        if not backups:
            return "", ""
        if any(backup == "unavailable" for backup in backups):
            return list_backup_label("unavailable"), "unavailable"
        if any(backup == "missing" for backup in backups):
            color = "linked" if gui_state(definition["id"]) in {"linked", "remove:completed"} else "broken"
            return list_backup_label("missing"), color
        old = next((backup for backup in backups if backup.startswith("old:")), "")
        if old:
            return list_backup_label(old), "broken"
        recent = next((backup for backup in backups if backup.startswith("recent:")), "")
        if recent:
            return list_backup_label(recent), "present"
        return "", ""

    def _upgrade_label(definition: dict) -> tuple[str, str]:
        # Format upgrade freshness for compact GUI display
        if definition["id"] == "dom0":
            return _dom0_health("upgrade", "upgraded")
        if gui_state(definition["id"]) == "linked":
            return "", ""
        status = status_by_kuhb.get(definition["id"])
        updates = [kuh.update for kuh in status.kuhs if kuh.update] if status is not None else []
        if not updates:
            return "never", "never"
        if any(update.startswith("upgradable") for update in updates):
            return _upgrade_age(definition), "upgradable"
        return _upgrade_age(definition), "upgraded"

    def _display_type(definition: dict) -> str:
        # Convert internal KUHB type ids into user-facing labels
        return {
            "app": "AppVM",
            "sta": "StandaloneVM",
            "udp": "Unnamed DisposableVM",
            "ndp": "Named DisposableVM",
        }.get(str(definition.get("type", "unknown")), str(definition.get("type", "unknown")))

    def _status_block(definition: dict, state_text: str | None, show_health: bool, state_color: str | None = None, show_order: bool = True):
        # Build the lifecycle/status column for one resolved kuh
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=34)
        status_box.set_halign(Gtk.Align.END)
        kuhb_id = definition["id"]
        if state_text:
            if busy(kuhb_id, "state"):
                status_box.pack_start(_right_spinner(116), False, False, 0)
            else:
                state = Gtk.Label(label=state_text, xalign=0.5)
                if definition.get("_broken"):
                    state.set_markup("<b>BROKEN !!!</b>")
                    _style(state, "kuhbs-state-badge", "kuhbs-list-red")
                else:
                    _style(state, "kuhbs-state-badge", _list_color_class(state_color or gui_state(kuhb_id)))
                state.set_width_chars(12)
                state.set_max_width_chars(12)
                _one_line(state)
                status_box.pack_start(_fixed_cell(state, 116), False, False, 0)
        if show_health:
            if busy(kuhb_id, "backup"):
                status_box.pack_start(_right_spinner(104), False, False, 0)
            else:
                backup, backup_color = _backup_label(definition)
                status_box.pack_start(_right_meta(backup, 104, 11, backup_color), False, False, 0)
            if busy(kuhb_id, "upgrade"):
                status_box.pack_start(_right_spinner(98), False, False, 0)
            else:
                upgrade, upgrade_color = _upgrade_label(definition)
                status_box.pack_start(_right_meta(upgrade, 98, 10, upgrade_color), False, False, 0)
            if show_order:
                status_box.pack_start(_right_meta(str(definition.get("order", 9999)), 68, 7), False, False, 0)
        elif "_status" in definition:
            # Invisible cells preserve the State column for TemplateVM rows without health metadata
            status_box.pack_start(_right_meta("", 104, 11), False, False, 0)
            status_box.pack_start(_right_meta("", 98, 10), False, False, 0)
            if show_order:
                status_box.pack_start(_right_meta("", 68, 7), False, False, 0)
        return status_box

    def make_card(definition: dict, root: Path, selected_card: bool = False, status_text: str | None = None, footer_text: str | None = None, action_button=None, show_health: bool = True, state_color: str | None = None, show_order: bool = True) -> Gtk.EventBox:
        # List rows keep scan order stable: identity on the left, state and sortable facts on the right
        kuhb_id = definition["id"]
        broken = bool(definition.get("_broken"))
        state = "broken" if broken else gui_state(kuhb_id)
        frame = Gtk.Frame()
        _style(frame, "kuhbs-row-selected" if selected_card else "kuhbs-row")
        frame.set_shadow_type(Gtk.ShadowType.NONE)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        row.set_margin_top(6)
        row.set_margin_bottom(6)
        row.set_margin_start(10)
        row.set_margin_end(10)
        row.pack_start(_kuhb_icon(definition, root), False, False, 0)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title = Gtk.Label(xalign=0)
        title.set_markup(f"<b>{escape(definition['id'])}</b> ({escape(_display_type(definition))})")
        _style(title, "kuhbs-title")
        _one_line(title)
        detail_text = definition.get("description") or definition["id"]
        detail = Gtk.Label(label=detail_text, xalign=0)
        _style(detail, "kuhbs-description")
        _one_line(detail)
        if broken:
            detail.set_selectable(True)
        text_box.pack_start(title, False, False, 0)
        text_box.pack_start(detail, False, False, 0)
        if footer_text is not None:
            footer = Gtk.Label(label=footer_text, xalign=0)
            _style(footer, "kuhbs-status")
            _one_line(footer)
            text_box.pack_start(footer, False, False, 0)
        row.pack_start(text_box, True, True, 0)

        state_text = "BROKEN !!!" if broken else (status_text if status_text is not None else display_state(state))
        row.pack_start(_status_block(definition, state_text, show_health and not broken, state_color, show_order), False, False, 0)
        if action_button is not None:
            row.pack_start(action_button, False, False, 0)
        frame.add(row)
        event = Gtk.EventBox()
        event.add(frame)
        event.kuhb_id = kuhb_id
        event.state = state
        return event

    def _definition_order(definition: dict) -> int:
        # Read the order value used to split My KUHBS from System KUHBS
        order = definition.get("order", 9999)
        return order if isinstance(order, int) else 9999

    def _is_system_kuhb(definition: dict) -> bool:
        # System KUHBs use low order values and are shown on the System tab
        order = _definition_order(definition)
        return 1 <= order <= 99

    def _is_my_kuhb(definition: dict) -> bool:
        # User KUHBs use normal order values and are shown on the My tab
        order = _definition_order(definition)
        return 100 <= order <= 1000

    def _kuhb_definitions(system: bool) -> list[dict]:
        # Return valid and broken linked definitions for the current tab in display order
        display_definitions = local_definition_cache + [
            _broken_card_definition(broken)
            for broken in local_broken_cache
        ]
        definitions = [
            definition
            for definition in sorted(display_definitions, key=_kuhb_sort_key)
            if (_is_system_kuhb(definition) if system else _is_my_kuhb(definition))
        ]
        return definitions

    def _qubes_os_vm_definitions() -> list[dict]:
        # Display every configured base name, including broken references excluded from the valid set
        template_names = set(base_template_vm_names(local_definition_cache))
        for broken in local_broken_cache:
            raw_template = (broken.definition or {}).get("template")
            if isinstance(raw_template, str):
                template_names.add(raw_template)
        definitions = [{
            "id": "dom0",
            "name": "dom0",
            "description": "Qubes OS AdminVM",
            "icon": "/usr/share/kuhbs/icons/dom0.svg",
            "type": "AdminVM",
            "_status": "Ready",
            "_status_color": gui_state("dom0"),
            # Dom0 has no KUHB dependency order, so its final health cell stays blank.
            "order": "",
        }]
        for vm_name in sorted(template_names):
            info = qubes_vm_cache.get(vm_name)
            if info is None:
                status, color, description = "Missing", "broken", "Referenced base TemplateVM is missing"
            elif info.klass != "TemplateVM":
                status, color, description = "Wrong type", "broken", f"Expected TemplateVM, found {info.klass}"
            else:
                status, color, description = "Ready", "present", "Qubes OS base TemplateVM"
            definitions.append({
                "id": vm_name,
                "name": vm_name,
                "description": description,
                "icon": "/usr/share/kuhbs/icons/dom0.svg",
                "type": "TemplateVM",
                "_status": status,
                "_status_color": color,
            })
        return definitions

    def _matches_search(definition: dict, query: str) -> bool:
        # Filter cards by simple lowercase text across common display fields
        if not query:
            return True
        haystack = " ".join(
            str(value)
            for value in (
                definition.get("id", ""),
                definition.get("name", ""),
                definition.get("description", ""),
                definition.get("type", ""),
            )
        ).lower()
        return query.lower() in haystack

    def refresh_kuhb_list(list_box, search_entry, selected_set: set[str], on_click, button_map: dict, all_button_map: dict, edit_button, system: bool) -> list[str]:
        # Rebuild one KUHB card list from current definitions and cached status
        for child in list_box.get_children():
            list_box.remove(child)
        list_box.pack_start(make_header_row(), False, False, 1)
        cards = []
        query = search_entry.get_text().strip()
        definitions = [definition for definition in _kuhb_definitions(system=system) if _matches_search(definition, query)]
        selected_set.intersection_update(definition["id"] for definition in definitions)
        visible_ids = [definition["id"] for definition in definitions]
        for definition in definitions:
            card = make_card(definition, ctx.kuhbs_root / definition["id"], definition["id"] in selected_set)
            card.selected = definition["id"] in selected_set
            card.connect("button-press-event", on_click)
            cards.append({"id": definition["id"], "state": card.state, "selected": card.selected})
            list_box.pack_start(card, False, False, 1)
        refresh_buttons(button_map, all_button_map, cards)
        # Dom0 has no KUHB YAML; repair mode further restricts Edit to a broken card
        edit_button.set_sensitive(edit_allowed(selected_set))
        window.show_all()
        return visible_ids

    def refresh_my() -> None:
        # Refresh the My KUHBS tab from local user definitions
        nonlocal visible_kuhb_ids
        if status_refresh_pending:
            return
        visible_kuhb_ids = refresh_kuhb_list(my_list, my_search, selected, on_card_click, buttons, all_buttons, my_edit_button, False)

    def refresh_system() -> None:
        # Refresh the System KUHBS tab from low-order definitions
        nonlocal visible_system_kuhb_ids
        if status_refresh_pending:
            return
        visible_system_kuhb_ids = refresh_kuhb_list(system_list, system_search, system_selected, on_system_card_click, system_buttons, system_all_buttons, system_edit_button, True)

    def refresh_qubes_os() -> None:
        # Rebuild dom0 and external base TemplateVM rows from the current validated snapshot
        nonlocal visible_qubes_os_vm_ids
        if status_refresh_pending:
            return
        for child in qubes_os_list.get_children():
            qubes_os_list.remove(child)
        qubes_os_list.pack_start(make_header_row(show_order=False), False, False, 1)
        query = qubes_os_search.get_text().strip()
        definitions = [definition for definition in _qubes_os_vm_definitions() if _matches_search(definition, query)]
        visible_qubes_os_vm_ids = [definition["id"] for definition in definitions]
        qubes_os_selected.intersection_update(visible_qubes_os_vm_ids)
        cards = []
        for definition in definitions:
            card = make_card(
                definition,
                Path("/usr/share/kuhbs/icons"),
                definition["id"] in qubes_os_selected,
                status_text=definition["_status"],
                state_color=definition["_status_color"],
                # Base templates have no KUHBS archive history; only dom0 owns these health cells.
                show_health=definition["id"] == "dom0",
                show_order=False,
            )
            card.selected = definition["id"] in qubes_os_selected
            card.connect("button-press-event", on_qubes_os_card_click)
            cards.append({"id": definition["id"], "state": card.state, "selected": card.selected})
            qubes_os_list.pack_start(card, False, False, 1)
        refresh_buttons(qubes_os_buttons, qubes_os_all_buttons, cards)
        window.show_all()

    def refresh_app_store() -> None:
        # Refresh the repo tab from local cloned repositories
        nonlocal visible_repo_sources, repo_linkable_sources, repo_unlinkable_sources, repo_source_kuhbs, repo_broken_cache
        if status_refresh_pending:
            return
        was_repair_mode = repair_mode()
        # Repo cards are repo KUHB directories that can be linked into My KUHBS
        query = app_search.get_text().strip()
        for child in app_list.get_children():
            app_list.remove(child)
        app_list.pack_start(make_header_row(repository=True), False, False, 1)
        visible_repo_sources = []
        visible_repo_ids = set()
        repo_linkable_sources = set()
        repo_unlinkable_sources = set()
        repo_source_kuhbs = {}
        repo_broken_cache = []
        for repo_root in repository._repo_dirs(ctx):
            repo_id = repo_root.relative_to(ctx.repos_root).as_posix()
            repo_definitions = []
            for kuhb_root in repository._repo_kuhb_dirs(repo_root):
                kuhb_yml = kuhb_root / "kuhb.yml"
                broken_candidate = False
                candidate_definition = None
                try:
                    definition = validate_kuhb_file(
                        ctx.defaults,
                        kuhb_yml,
                        raw_validator=repo_raw_validator,
                        resolved_validator=repo_resolved_validator,
                        qubes_vms=qubes_vm_cache,
                    )
                    candidate_definition = definition
                    validate_definition_set(
                        ctx.defaults,
                        [(kuhb_root.name, kuhb_yml, definition)],
                    )
                except ConfigValidationError as exc:
                    broken = BrokenKuhb(
                        kuhb_root.name,
                        kuhb_yml,
                        exc.issues,
                        exc.definition or candidate_definition,
                    )
                    repo_broken_cache.append(broken)
                    definition = _broken_card_definition(broken)
                    broken_candidate = True
                if _matches_search(definition, query) or query.lower() in repo_id.lower():
                    repo_definitions.append((definition, kuhb_root, broken_candidate))
            if query and not repo_definitions and query.lower() not in repo_id.lower():
                continue
            visible_repo_ids.add(repo_id)
            repo_event = make_repo_header_row(repo_id, repo_root, repo_id in selected_repos)
            repo_event.connect("button-press-event", on_repo_header_click)
            app_list.pack_start(repo_event, False, False, 1)
            for definition, kuhb_root, broken_candidate in sorted(repo_definitions, key=lambda item: _kuhb_sort_key(item[0])):
                link_source = f"{repo_id}/{kuhb_root.name}"
                target = ctx.kuhbs_root / definition["id"]
                linked = target.exists() or target.is_symlink()
                visible_repo_sources.append(link_source)
                repo_source_kuhbs[link_source] = definition["id"]
                if not linked and not broken_candidate:
                    repo_linkable_sources.add(link_source)
                # Repaints use the published state snapshot; the fresh CLI repeats authoritative preflight
                if linked and target.is_symlink() and target.resolve() == kuhb_root.resolve() and action_allowed(gui_state(definition["id"]), "unlink"):
                    repo_unlinkable_sources.add(link_source)
                card = make_card(
                    definition,
                    kuhb_root,
                    selected_card=link_source in repo_selected,
                    status_text="Linked" if linked else "Unlinked",
                    show_health=False,
                )
                card.source = link_source
                card.linked = linked
                card.connect("button-press-event", on_repo_card_click)
                app_list.pack_start(card, False, False, 1)
        selected_repos.intersection_update(visible_repo_ids)
        repo_selected.intersection_update(visible_repo_sources)
        running = actions_blocked() or repair_mode()
        install_button.set_sensitive(not running)
        repo_link_button.set_sensitive(not running and bool(repo_selected) and repo_selected <= repo_linkable_sources)
        repo_unlink_button.set_sensitive(not running and bool(repo_selected) and repo_selected <= repo_unlinkable_sources)
        update_button.set_sensitive(not running and bool(selected_repos))
        repo_edit_button.set_sensitive(repo_edit_allowed())
        if repair_mode() != was_repair_mode:
            # Repository validation can enter or leave global repair mode after the other tabs painted
            refresh_my()
            refresh_system()
            refresh_qubes_os()
        window.show_all()
        alert_broken_changes((*local_broken_cache, *repo_broken_cache))

    def clear_selection() -> None:
        # Clear selected cards after Escape or blank-space clicks
        nonlocal selection_anchor, system_selection_anchor, qubes_os_selection_anchor, repo_selection_anchor
        selected.clear()
        system_selected.clear()
        qubes_os_selected.clear()
        repo_selected.clear()
        selected_repos.clear()
        selection_anchor = None
        system_selection_anchor = None
        qubes_os_selection_anchor = None
        repo_selection_anchor = None
        refresh_my()
        refresh_system()
        refresh_qubes_os()
        refresh_app_store()

    def select_all_visible() -> None:
        # Select only cards currently visible in the active tab
        nonlocal selection_anchor, system_selection_anchor, qubes_os_selection_anchor, repo_selection_anchor
        page = notebook.get_current_page()
        if page == 0:
            selected.clear()
            selected.update(visible_kuhb_ids)
            selection_anchor = visible_kuhb_ids[0] if visible_kuhb_ids else None
            refresh_my()
        elif page == 1:
            system_selected.clear()
            system_selected.update(visible_system_kuhb_ids)
            system_selection_anchor = visible_system_kuhb_ids[0] if visible_system_kuhb_ids else None
            refresh_system()
        elif page == 2:
            qubes_os_selected.clear()
            qubes_os_selected.update(visible_qubes_os_vm_ids)
            qubes_os_selection_anchor = visible_qubes_os_vm_ids[0] if visible_qubes_os_vm_ids else None
            refresh_qubes_os()
        elif page == 3:
            selected_repos.clear()
            repo_selected.clear()
            repo_selected.update(visible_repo_sources)
            repo_selection_anchor = visible_repo_sources[0] if visible_repo_sources else None
            refresh_app_store()

    def clear_selection_on_blank(_widget, event):
        # Treat clicks on blank page space as selection clearing
        if getattr(event, "button", None) != 1:
            return False
        clear_selection()
        return True

    def on_repo_header_click(header, event):
        # Toggle repo selection when the user clicks a repo header row
        nonlocal repo_selection_anchor
        repo_selected.clear()
        repo_selection_anchor = None
        if event.state & Gdk.ModifierType.CONTROL_MASK:
            if header.repo_id in selected_repos:
                selected_repos.remove(header.repo_id)
            else:
                selected_repos.add(header.repo_id)
        else:
            selected_repos.clear()
            selected_repos.add(header.repo_id)
        refresh_app_store()
        return True

    def on_repo_card_click(card, event):
        # Toggle one repo KUHB card and update repo action buttons
        nonlocal repo_selection_anchor
        selected_repos.clear()
        # Repo KUHB selection mirrors My KUHBS: click, Ctrl-click, Shift-click range
        if event.state & Gdk.ModifierType.SHIFT_MASK and repo_selection_anchor in visible_repo_sources and card.source in visible_repo_sources:
            start = visible_repo_sources.index(repo_selection_anchor)
            end = visible_repo_sources.index(card.source)
            low, high = sorted((start, end))
            repo_selected.update(visible_repo_sources[low:high + 1])
        elif event.state & Gdk.ModifierType.CONTROL_MASK:
            if card.source in repo_selected:
                repo_selected.remove(card.source)
            else:
                repo_selected.add(card.source)
            repo_selection_anchor = card.source
        else:
            repo_selected.clear()
            repo_selected.add(card.source)
            repo_selection_anchor = card.source
        refresh_app_store()
        return True

    def _select_kuhb(card, event, selected_set: set[str], visible_ids: list[str], anchor: str | None) -> str | None:
        # Toggle one KUHB card and keep selection lists mutually exclusive
        if event.state & Gdk.ModifierType.SHIFT_MASK and anchor in visible_ids and card.kuhb_id in visible_ids:
            start = visible_ids.index(anchor)
            end = visible_ids.index(card.kuhb_id)
            low, high = sorted((start, end))
            selected_set.update(visible_ids[low:high + 1])
            return anchor
        if event.state & Gdk.ModifierType.CONTROL_MASK:
            if card.kuhb_id in selected_set:
                selected_set.remove(card.kuhb_id)
            else:
                selected_set.add(card.kuhb_id)
            return card.kuhb_id
        selected_set.clear()
        selected_set.add(card.kuhb_id)
        return card.kuhb_id

    def on_card_click(card, event):
        # Handle clicks on normal My KUHBS cards
        nonlocal selection_anchor
        selection_anchor = _select_kuhb(card, event, selected, visible_kuhb_ids, selection_anchor)
        refresh_my()
        return True

    def on_system_card_click(card, event):
        # Handle clicks on System KUHBS cards without editing local selection rules
        nonlocal system_selection_anchor
        system_selection_anchor = _select_kuhb(card, event, system_selected, visible_system_kuhb_ids, system_selection_anchor)
        refresh_system()
        return True

    def on_qubes_os_card_click(card, event):
        # Keep Qubes OS VM selection isolated from both KUHB pages
        nonlocal qubes_os_selection_anchor
        qubes_os_selection_anchor = _select_kuhb(card, event, qubes_os_selected, visible_qubes_os_vm_ids, qubes_os_selection_anchor)
        refresh_qubes_os()
        return True

    def kuhbs_command(*parts: str) -> list[str]:
        # GUI actions execute the installed CLI wrapper; missing wrapper is an installation error.
        command = ["/usr/bin/kuhbs", *parts]
        if not Path(command[0]).exists():
            raise FileNotFoundError(f"KUHBS CLI wrapper not found: {command[0]}")
        if args.defaults:
            command.insert(1, "--defaults")
            command.insert(2, str(args.defaults))
        return command

    def start_terminal_command(command_args: list[str], title_parts, affected=(), field: str | None = None, track: bool = False, display_commands=()) -> None:
        # No CLI terminal may start while the GUI is exposing only broken-KUHB repair
        if repair_mode():
            return
        wrapper = "/usr/share/kuhbs/kuhbs-gui-run-command.sh"
        command_parts = [wrapper, "env", "KUHBS_FROM_GUI=1", *command_args]
        display_text = "\n".join(" ".join(parts) for parts in display_commands)
        if display_text:
            command_parts = ["env", f"KUHBS_GUI_DISPLAY_COMMANDS={display_text}", *command_parts]
        command = " ".join(quote(part) for part in command_parts)
        title = "KUHBS " + " ".join(str(part) for part in title_parts)
        prefix = dom0_terminal_command_prefix(ctx, title=title)
        terminal_path = prefix[0]
        if terminal_path.endswith("/xfce4-terminal") or terminal_path == "xfce4-terminal":
            terminal_args = [prefix[0], "--disable-server", *prefix[1:], "--command", command]
        elif terminal_path.endswith("/xterm") or terminal_path == "xterm":
            terminal_args = [*prefix, "-e", "bash", "-lc", command]
        else:
            raise RuntimeError(f"unsupported terminal command syntax for {terminal_path}")
        process = subprocess.Popen(terminal_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        affected_ids = tuple(affected)
        mark_busy(affected_ids, field)
        if field is not None or track:
            running_ops.append({"process": process, "kuhbs": affected_ids, "field": field})
            refresh_my()
            refresh_system()
            refresh_qubes_os()
            refresh_app_store()
            GLib.timeout_add(250, poll_operations)

    def start_kuhbs_command(*parts: str, affected=(), field: str | None = None, track: bool = False) -> None:
        # Start one CLI operation through the repair-aware terminal boundary
        display_command = [["kuhbs", *parts]]
        start_terminal_command(kuhbs_command(*parts), parts, affected=affected, field=field, track=track, display_commands=display_command)

    def poll_operations() -> bool:
        # Poll child terminals and refresh GUI state when any of them exit
        nonlocal status_refresh_pending
        changed = False
        for op in list(running_ops):
            if op["process"].poll() is None:
                continue
            running_ops.remove(op)
            unmark_busy(op["kuhbs"], op["field"])
            changed = True
        if changed:
            # Block every alternate repaint path until the callback replaces the pre-operation cache
            status_refresh_pending = True
            schedule_status_refresh()
        return bool(running_ops)

    def poll_editors() -> bool:
        # A standalone Gedit process marks the exact point when edited definitions should be revalidated
        closed = False
        for process in list(editor_processes):
            if process.poll() is None:
                continue
            editor_processes.remove(process)
            closed = True
        if closed:
            schedule_status_refresh()
        return bool(editor_processes)

    def confirm_action(title: str, text: str) -> bool:
        # Ask for confirmation before destructive or broad single-card actions
        dialog = Gtk.MessageDialog(
            transient_for=window,
            flags=0,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO,
            text=title,
        )
        dialog.format_secondary_text(text)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.YES

    def open_kuhb_editor(kuhb_root: Path) -> None:
        # Pin both browser roots because Gedit otherwise remembers broad locations such as the user's home directory
        browser_settings = Gio.Settings.new("org.gnome.gedit.plugins.filebrowser")
        kuhb_uri = kuhb_root.as_uri()
        browser_settings.set_string("root", kuhb_uri)
        browser_settings.set_string("virtual-root", kuhb_uri)
        Gio.Settings.sync()
        # Track this standalone process without blocking other GUI actions while the editor remains open
        start_poll = not editor_processes
        process = subprocess.Popen(
            ["gedit", "--standalone", "kuhb.yml"],
            cwd=kuhb_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        editor_processes.append(process)
        if start_poll:
            GLib.timeout_add(250, poll_editors)

    def edit_selected(selected_set: set[str]) -> None:
        # Local and system cards resolve through their active My KUHBS path before opening the shared editor
        if not edit_allowed(selected_set):
            return
        kuhb_id = next(iter(selected_set))
        kuhb_root = (ctx.kuhbs_root / kuhb_id).resolve()
        open_kuhb_editor(kuhb_root)

    my_edit_button.connect("clicked", lambda _button: edit_selected(selected))
    system_edit_button.connect("clicked", lambda _button: edit_selected(system_selected))

    def action_field(action: str) -> str:
        # Both ordinary and -all commands paint the column owned by their base action
        base_action = action.removesuffix("-all")
        return base_action if base_action in {"backup", "upgrade"} else "state"

    def run_action(action: str, selected_set: set[str]) -> None:
        # Button sensitivity is advisory; the fresh CLI repeats strict shared admission
        targets = sorted(selected_set)
        if not targets or any_busy(targets) or actions_blocked():
            return
        if action in {"remove", "restore"} and not confirm_action(
            f"{action.capitalize()} selected KUHBS?",
            "This will run for:\n" + "\n".join(targets),
        ):
            return
        # One real multi-target CLI command owns dependency ordering, preflight and dom0-last restore
        start_kuhbs_command(action, *targets, affected=targets, field=action_field(action))

    for action, button in buttons.items():
        button.connect("clicked", lambda _button, action=action: run_action(action, selected))
    for action, button in system_buttons.items():
        button.connect("clicked", lambda _button, action=action: run_action(action, system_selected))
    for action, button in qubes_os_buttons.items():
        button.connect("clicked", lambda _button, action=action: run_action(action, qubes_os_selected))


    def run_all_action(action: str) -> None:
        # Replanning at click time prevents a stale enabled button from dispatching an obsolete target set
        if actions_blocked():
            return
        # Storage helpers have no KUHB target set and therefore stay outside all-action planning
        if action in {"backup-mount", "backup-umount"}:
            start_kuhbs_command(action, track=True)
            return
        plan = gui_action_plan(action)
        if plan.is_empty():
            return
        # Upgrade-All paints every target row while the one global CLI terminal owns execution
        targets = [definition["id"] for definition in plan.runnable] + list(plan.qubes_templates)
        if action in {"backup-all", "restore-all"}:
            # Archive-All can expand each parent plus dom0, so one global terminal owns its busy state
            start_kuhbs_command(action, track=True)
            return
        start_kuhbs_command(action, affected=targets, field=action_field(action))

    for action, button in all_buttons.items():
        button.connect("clicked", lambda _button, action=action: run_all_action(action))
    for action, button in system_all_buttons.items():
        button.connect("clicked", lambda _button, action=action: run_all_action(action))
    for action, button in qubes_os_all_buttons.items():
        button.connect("clicked", lambda _button, action=action: run_all_action(action))

    def run_repo_edit(_button) -> None:
        # Repo cards open their checkout directly, whether or not the KUHB is currently linked
        if not repo_edit_allowed():
            return
        source = next(iter(repo_selected))
        open_kuhb_editor((ctx.repos_root / source).resolve())

    repo_edit_button.connect("clicked", run_repo_edit)

    def run_repo_update(_button) -> None:
        # Repo update acts on explicitly selected repo headers; one terminal avoids contending on the global repos lock
        repos = sorted(selected_repos)
        if not repos:
            return
        commands = [" ".join(quote(part) for part in kuhbs_command("update-repo", repo_id)) for repo_id in repos]
        script = "rc=0; " + " ".join(f"{command} || rc=$?;" for command in commands) + " exit $rc"
        display_commands = [["kuhbs", "update-repo", repo_id] for repo_id in repos]
        start_terminal_command(["bash", "-lc", script], ["update-repo", *repos], track=True, display_commands=display_commands)

    update_button.connect("clicked", run_repo_update)

    def run_repo_link(_button) -> None:
        # Send one batch so the CLI validates all links and restarts desktop services once
        link_sources = sorted(repo_selected if repo_selected <= repo_linkable_sources else [])
        if not link_sources:
            return
        start_kuhbs_command("link", *link_sources, track=True)

    repo_link_button.connect("clicked", run_repo_link)

    def run_repo_unlink(_button) -> None:
        # Send one batch so the CLI preflights all removals and restarts desktop services once
        unlink_sources = sorted(repo_selected if repo_selected <= repo_unlinkable_sources else [])
        if not unlink_sources:
            return
        affected_ids = [repo_source_kuhbs[source] for source in unlink_sources]
        start_kuhbs_command("unlink", *unlink_sources, affected=affected_ids, track=True)

    repo_unlink_button.connect("clicked", run_repo_unlink)

    def run_install(_button) -> None:
        # Repo add uses a dedicated dialog; the Repos search field stays search-only
        dialog = Gtk.Dialog(title="Add repo", transient_for=window, flags=0)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Add", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(8)
        url_label = Gtk.Label(label="Repo URL", xalign=0)
        url_label.set_margin_top(12)
        url_label.set_margin_start(12)
        url_label.set_margin_end(12)
        url_entry = Gtk.Entry()
        url_entry.set_placeholder_text("https://github.com/foobar/bla")
        url_entry.set_width_chars(50)
        url_entry.set_margin_start(12)
        url_entry.set_margin_end(12)
        branch_label = Gtk.Label(label="Branch", xalign=0)
        branch_label.set_margin_start(12)
        branch_label.set_margin_end(12)
        branch_entry = Gtk.Entry()
        branch_entry.set_text(ctx.defaults["repos"]["branch"])
        branch_entry.set_width_chars(50)
        branch_entry.connect("activate", lambda _entry: dialog.response(Gtk.ResponseType.OK))
        branch_entry.set_margin_start(12)
        branch_entry.set_margin_end(12)
        branch_entry.set_margin_bottom(12)
        box.pack_start(url_label, False, False, 0)
        box.pack_start(url_entry, False, False, 0)
        box.pack_start(branch_label, False, False, 0)
        box.pack_start(branch_entry, False, False, 0)
        dialog.show_all()
        response = dialog.run()
        source = url_entry.get_text().strip()
        branch = branch_entry.get_text().strip()
        dialog.destroy()
        if response == Gtk.ResponseType.OK and source:
            if actions_blocked():
                return
            command_args = [source, branch] if branch else [source]
            start_kuhbs_command("repo-add", *command_args, track=True)

    install_button.connect("clicked", run_install)
    my_search.connect("search-changed", lambda _entry: refresh_my())
    system_search.connect("search-changed", lambda _entry: refresh_system())
    qubes_os_search.connect("search-changed", lambda _entry: refresh_qubes_os())
    app_search.connect("search-changed", lambda _entry: refresh_app_store())
    for scrolled in (my_scrolled, system_scrolled, qubes_os_scrolled, app_scrolled):
        scrolled.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        scrolled.connect("button-press-event", clear_selection_on_blank)
    refresh_status_cache()
    refresh_my()
    refresh_system()
    refresh_qubes_os()
    refresh_app_store()
    startup_validation_dialog.destroy()
    window.set_deletable(True)
    notebook.set_sensitive(True)
    window.show_all()
    Gtk.main()
    return 0


def main(argv: list[str] | None = None) -> int:
    # The GUI and a manual CLI never own the same KUHBS state tree at once
    if CLI_MARKER.exists():
        print("Wait for the running KUHBS CLI to finish before opening the GUI")
        return 1
    if GUI_MARKER.exists():
        print("The KUHBS GUI is already running")
        return 1
    GUI_MARKER.parent.mkdir(parents=True, exist_ok=True)
    GUI_MARKER.touch()
    try:
        return _run_gui(argv)
    finally:
        GUI_MARKER.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
