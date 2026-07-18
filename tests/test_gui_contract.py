# Purpose: Regression tests for GTK behavior that lives inside gui.main closures
# Scope: Inspect the parsed nested functions without requiring PyGObject in the test VM
from pathlib import Path
from types import SimpleNamespace
import ast
from contextlib import redirect_stderr
from io import StringIO
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch

import kuhbs.gui as gui
from kuhbs.gui import _broken_card_definition, _broken_fingerprint, _new_broken_entries, _show_copyable_error, _validate_gui_config
from kuhbs.model import display_state
from kuhbs.validation import BrokenKuhb, ConfigIssue, ConfigValidationError


GUI_PATH = Path(__file__).parents[1] / "kuhbs/gui.py"
INSTALL_PATH = Path(__file__).parents[1] / "install/install.sh"
DOM0_ICON_TEMPLATE = Path(__file__).parents[1] / "install/templates/usr/share/kuhbs/icons/dom0.svg"


def nested_function_source(name: str) -> str:
    # AST lookup keeps these tests tied to named GUI behavior instead of unrelated matching text
    source = GUI_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next((node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name), None)
    return ast.get_source_segment(source, function) if function is not None else ""


class GuiContractTests(unittest.TestCase):
    def run_validation(self, defaults_path: Path, *, parent=None):
        # Execute worker callbacks immediately while keeping production's thread/GLib boundary explicit
        class ImmediateThread:
            def __init__(self, *, target, daemon):
                self.target = target

            def start(self):
                self.target()

            def join(self):
                return None

        gtk = SimpleNamespace(ResponseType=SimpleNamespace(OK=1))
        glib = SimpleNamespace(idle_add=lambda callback, *args: callback(*args))
        with patch("kuhbs.gui.threading.Thread", ImmediateThread):
            return _validate_gui_config(gtk, glib, defaults_path, parent=parent)

    def test_fresh_dom0_gate_displays_ready_in_green(self):
        # Dom0 exists inherently, so its pre-operation lifecycle gate needs a normal green card label
        self.assertEqual(display_state("dom0"), "Ready")
        self.assertIn('"dom0"', nested_function_source("_list_color_class"))

    def test_packaged_dom0_icon_is_square_svg(self):
        # A square canvas keeps GTK from shrinking the official Qubes mark differently from KUHB icons
        root = ET.parse(DOM0_ICON_TEMPLATE).getroot()

        self.assertEqual(root.get("width"), root.get("height"))
        view_box = [float(value) for value in root.get("viewBox", "").split()]
        self.assertEqual(len(view_box), 4)
        self.assertEqual(view_box[2], view_box[3])

    def test_installer_copies_dom0_icon_to_shared_assets(self):
        # The synthetic dom0 card has no KUHB directory, so its icon must be installed with KUHBS itself
        installer = INSTALL_PATH.read_text(encoding="utf-8")

        self.assertIn("templates/usr/share/kuhbs/icons/.", installer)
        self.assertIn("/usr/share/kuhbs/icons", installer)

    def test_dom0_card_uses_installed_qubes_icon(self):
        # The synthetic definition must identify the bundled asset instead of reaching GTK's generic fallback
        displayed_definitions = nested_function_source("_qubes_os_vm_definitions")

        self.assertIn('"icon": "/usr/share/kuhbs/icons/dom0.svg"', displayed_definitions)

    def test_live_validation_notice_uses_standard_modal_message_dialog(self):
        notice_function = getattr(gui, "_show_validation_notice", None)
        self.assertIsNotNone(notice_function)
        dialog = Mock()
        callbacks = {}

        def connect(signal, callback):
            callbacks[signal] = callback
            return 10

        dialog.connect.side_effect = connect

        gtk = SimpleNamespace(
            DialogFlags=SimpleNamespace(MODAL=1),
            MessageDialog=Mock(return_value=dialog),
            MessageType=SimpleNamespace(INFO=2),
            ButtonsType=SimpleNamespace(NONE=0),
        )
        parent = Mock()

        result = notice_function(gtk, parent)

        self.assertIs(result, dialog)
        gtk.MessageDialog.assert_called_once_with(
            transient_for=parent,
            flags=gtk.DialogFlags.MODAL,
            message_type=gtk.MessageType.INFO,
            buttons=gtk.ButtonsType.NONE,
            text="Validating configuration",
        )
        dialog.format_secondary_text.assert_called_once_with(
            "All actions are blocked. Please wait..."
        )
        dialog.set_deletable.assert_called_once_with(False)
        dialog.set_resizable.assert_called_once_with(False)
        self.assertTrue(callbacks["delete-event"](dialog, None))

    def test_validation_runs_standard_dialog_while_worker_validates(self):
        validation = nested_function_source("_validate_gui_config")

        self.assertIn("threading.Thread", validation)
        self.assertIn("dialog.run()", validation)
        self.assertIn("GLib.idle_add(dialog.response", validation)

    def test_worker_start_failure_closes_notice_reports_and_aborts(self):
        pending = []
        notice = Mock()
        worker = Mock()
        worker.start.side_effect = OSError("thread unavailable")

        def idle_add(callback, *args):
            pending.append((callback, args))
            return 1

        def run_dialog():
            while pending:
                callback, args = pending.pop(0)
                callback(*args)

        notice.run.side_effect = run_dialog
        gtk = SimpleNamespace(ResponseType=SimpleNamespace(OK=1))
        glib = SimpleNamespace(idle_add=idle_add)

        with patch("kuhbs.gui._show_validation_notice", return_value=notice):
            with patch("kuhbs.gui.threading.Thread", return_value=worker):
                with patch("kuhbs.gui._show_copyable_error") as show_error:
                    result = _validate_gui_config(gtk, glib, Path("/tmp/defaults.yml"))

        self.assertIsNone(result)
        notice.response.assert_called_once_with(gtk.ResponseType.OK)
        notice.destroy.assert_called_once_with()
        worker.join.assert_not_called()
        show_error.assert_called_once_with(
            gtk,
            "KUHBS validation failed",
            "thread unavailable",
            parent=None,
        )

    def test_live_validation_failure_reports_and_closes_the_gui(self):
        validate_live = getattr(gui, "_validate_live_gui_config", None)
        self.assertIsNotNone(validate_live)
        parent = Mock()
        gtk = object()
        error = OSError("read failed")

        with patch("kuhbs.gui._validate_gui_config", side_effect=error):
            with patch("kuhbs.gui._show_copyable_error") as show_error:
                result = validate_live(gtk, object(), Path("/tmp/defaults.yml"), parent)

        self.assertIsNone(result)
        show_error.assert_called_once_with(
            gtk,
            "KUHBS validation failed",
            "read failed",
            parent=parent,
        )
        parent.destroy.assert_called_once_with()

    def test_successful_validation_returns_the_open_notice_to_its_caller(self):
        validated = SimpleNamespace(defaults={}, kuhb_definitions=(), broken_kuhbs=())
        notice = Mock()
        defaults_path = Path("/tmp/defaults.yml")
        with patch("kuhbs.gui._show_validation_notice", return_value=notice):
            with patch("kuhbs.gui.inspect_startup_config", return_value=validated) as inspect:
                result = self.run_validation(defaults_path, parent=Mock())

        self.assertEqual(result, (validated, notice))
        inspect.assert_called_once_with(defaults_path, check_qubes=True)
        notice.destroy.assert_not_called()

    def test_non_kuhb_validation_issue_aborts_instead_of_entering_repair_mode(self):
        kuhb_path = Path("/active/bad/kuhb.yml")
        state_path = Path("/states/bad/create")
        broken = BrokenKuhb("bad", kuhb_path, (ConfigIssue(state_path, "invalid lifecycle state"),))
        validated = SimpleNamespace(defaults={}, kuhb_definitions=(), broken_kuhbs=(broken,))
        notice = Mock()
        parent = Mock()

        with patch("kuhbs.gui._show_validation_notice", return_value=notice):
            with patch("kuhbs.gui.inspect_startup_config", return_value=validated):
                with patch("kuhbs.gui._show_copyable_error") as show_error:
                    result = self.run_validation(Path("/tmp/defaults.yml"), parent=parent)

        self.assertIsNone(result)
        show_error.assert_called_once_with(
            SimpleNamespace(ResponseType=SimpleNamespace(OK=1)),
            "KUHBS validation failed",
            "Configuration invalid\n/states/bad/create: invalid lifecycle state",
            parent=parent,
        )
        parent.destroy.assert_called_once_with()

    def test_live_validation_notice_runs_before_worker_validation(self):
        events = []
        pending = []
        notice = Mock()
        validated = SimpleNamespace(defaults={}, kuhb_definitions=(), broken_kuhbs=())

        class ImmediateThread:
            def __init__(self, *, target, daemon):
                self.target = target

            def start(self):
                self.target()

            def join(self):
                events.append("join")

        def show_notice(_gtk, _parent):
            events.append("show")
            return notice

        def run_dialog():
            events.append("run")
            while pending:
                callback, args = pending.pop(0)
                callback(*args)

        def idle_add(callback, *args):
            pending.append((callback, args))
            return 1

        def validate(_path, *, check_qubes=False):
            self.assertTrue(check_qubes)
            events.append("validate")
            return validated

        notice.run.side_effect = run_dialog
        notice.response.side_effect = lambda _response: events.append("response")
        notice.destroy.side_effect = lambda: events.append("destroy")
        gtk = SimpleNamespace(ResponseType=SimpleNamespace(OK=1))
        glib = SimpleNamespace(idle_add=idle_add)

        with patch("kuhbs.gui._show_validation_notice", side_effect=show_notice):
            with patch("kuhbs.gui.inspect_startup_config", side_effect=validate):
                with patch("kuhbs.gui.threading.Thread", ImmediateThread):
                    result = _validate_gui_config(
                        gtk,
                        glib,
                        Path("/tmp/defaults.yml"),
                        parent=Mock(),
                    )

        self.assertEqual(result, (validated, notice))
        self.assertEqual(
            events,
            ["show", "run", "validate", "response", "join"],
        )

    def test_global_validation_error_closes_notice_reports_and_aborts(self):
        issue = ConfigIssue(Path("/tmp/defaults.yml"), "paths.kuhbs is required")
        notice = Mock()
        parent = Mock()

        with patch("kuhbs.gui._show_validation_notice", return_value=notice):
            with patch(
                "kuhbs.gui.inspect_startup_config",
                side_effect=ConfigValidationError([issue]),
            ):
                with patch("kuhbs.gui._show_copyable_error") as show_error:
                    result = self.run_validation(Path("/tmp/defaults.yml"), parent=parent)

        self.assertIsNone(result)
        notice.destroy.assert_called_once_with()
        show_error.assert_called_once_with(
            SimpleNamespace(ResponseType=SimpleNamespace(OK=1)),
            "KUHBS validation failed",
            "Configuration invalid\n/tmp/defaults.yml: paths.kuhbs is required",
            parent=parent,
        )
        parent.destroy.assert_called_once_with()

    def test_unexpected_validation_failure_closes_notice_reports_and_aborts(self):
        notice = Mock()
        parent = Mock()
        with patch("kuhbs.gui._show_validation_notice", return_value=notice):
            with patch("kuhbs.gui.inspect_startup_config", side_effect=OSError("read failed")):
                with patch("kuhbs.gui._show_copyable_error") as show_error:
                    result = self.run_validation(Path("/tmp/defaults.yml"), parent=parent)

        self.assertIsNone(result)
        notice.destroy.assert_called_once_with()
        show_error.assert_called_once_with(
            SimpleNamespace(ResponseType=SimpleNamespace(OK=1)),
            "KUHBS validation failed",
            "read failed",
            parent=parent,
        )
        parent.destroy.assert_called_once_with()

    def test_startup_validation_shows_the_standard_notice(self):
        validated = SimpleNamespace(defaults={}, kuhb_definitions=(), broken_kuhbs=())
        notice = Mock()
        with patch("kuhbs.gui._show_validation_notice", return_value=notice) as show_notice:
            with patch("kuhbs.gui.inspect_startup_config", return_value=validated):
                result = self.run_validation(Path("/tmp/defaults.yml"))

        self.assertEqual(result, (validated, notice))
        self.assertIsNone(show_notice.call_args.args[1])
        notice.run.assert_called_once_with()
        notice.destroy.assert_not_called()

    def test_validation_error_has_no_retry_path(self):
        validate_gui = nested_function_source("_validate_gui_config")

        self.assertNotIn("while True", validate_gui)
        self.assertNotIn("retry=True", validate_gui)
        self.assertIn("inspect_startup_config(defaults_path, check_qubes=True)", validate_gui)

    def test_copyable_error_uses_ok_or_close_without_retry(self):
        response_type = SimpleNamespace(OK=-5, CLOSE=-7)
        cases = (
            ("ok-only", {"ok_only": True}, response_type.OK, "OK", response_type.OK),
            ("close-only", {}, response_type.CLOSE, "Close", response_type.CLOSE),
        )
        for name, options, response, button_label, button_response in cases:
            with self.subTest(name=name):
                dialog = Mock()
                dialog.run.return_value = response
                # Mock widgets keep this executable without requiring a display or PyGObject in the test VM
                gtk = SimpleNamespace(
                    DialogFlags=SimpleNamespace(MODAL=1),
                    Dialog=Mock(return_value=dialog),
                    ResponseType=response_type,
                    Label=Mock(),
                    ScrolledWindow=Mock(),
                    PolicyType=SimpleNamespace(AUTOMATIC=1),
                    TextView=Mock(),
                    WrapMode=SimpleNamespace(NONE=0),
                )

                result = _show_copyable_error(gtk, "title", "text", **options)

                self.assertIsNone(result)
                dialog.add_button.assert_called_once_with(button_label, button_response)
                dialog.destroy.assert_called_once_with()

    def test_copyable_gui_errors_are_also_written_to_stderr(self):
        dialog = Mock()
        dialog.run.return_value = -7
        gtk = SimpleNamespace(
            DialogFlags=SimpleNamespace(MODAL=1),
            Dialog=Mock(return_value=dialog),
            ResponseType=SimpleNamespace(OK=-5, CLOSE=-7),
            Label=Mock(),
            ScrolledWindow=Mock(),
            PolicyType=SimpleNamespace(AUTOMATIC=1),
            TextView=Mock(),
            WrapMode=SimpleNamespace(NONE=0),
        )
        stderr = StringIO()

        with redirect_stderr(stderr):
            _show_copyable_error(gtk, "KUHBS status refresh failed", "state read failed")

        self.assertEqual(stderr.getvalue(), "KUHBS status refresh failed: state read failed\n")
        self.assertNotIn("stderr", nested_function_source("_show_validation_notice"))

    def test_closing_live_validation_error_closes_the_parent_gui(self):
        issue = ConfigIssue(Path("/tmp/defaults.yml"), "paths.kuhbs is required")
        parent = Mock()
        notice = Mock()
        with patch(
            "kuhbs.gui.inspect_startup_config",
            side_effect=ConfigValidationError([issue]),
        ):
            with patch("kuhbs.gui._show_validation_notice", return_value=notice, create=True):
                with patch("kuhbs.gui._show_copyable_error", return_value=False):
                    result = self.run_validation(
                        Path("/tmp/defaults.yml"),
                        parent=parent,
                    )

        self.assertIsNone(result)
        notice.destroy.assert_called_once_with()
        parent.destroy.assert_called_once_with()

    def test_validation_notice_closes_after_startup_and_live_status_refresh(self):
        main = nested_function_source("_run_gui")
        schedule = nested_function_source("schedule_status_refresh")

        # Paint the complete but disabled interface before validation starts
        initial_window = main.index('window = Gtk.Window(title="KUHBS GUI")')
        final_page = main.index('notebook.append_page(repos_box, Gtk.Label(label="Repos"))')
        notebook_disabled = main.index("notebook.set_sensitive(False)")
        initial_show = main.index("window.show_all()", initial_window)
        startup_validation = main.index("_validate_gui_config(Gtk, GLib, defaults_path, parent=window)")
        self.assertLess(initial_window, final_page)
        self.assertLess(final_page, notebook_disabled)
        self.assertLess(notebook_disabled, initial_show)
        self.assertLess(initial_show, startup_validation)
        self.assertLess(main.index("window.set_deletable(False)"), startup_validation)
        self.assertLess(startup_validation, main.index("ctx = OperationContext"))
        # Gtk.main_quit is connected only after the startup dialog's nested loop has finished
        self.assertLess(startup_validation, main.index('window.connect("destroy", Gtk.main_quit)'))
        startup_destroy = main.index("startup_validation_dialog.destroy()")
        self.assertLess(main.index("refresh_status_cache()"), startup_destroy)
        self.assertLess(startup_destroy, main.index("window.set_deletable(True)", startup_destroy))
        self.assertLess(startup_destroy, main.index("notebook.set_sensitive(True)", startup_destroy))
        normal_apply = schedule.index("status_refresh_pending = False")
        self.assertLess(normal_apply, schedule.index("refresh_my()", normal_apply))
        self.assertLess(schedule.index("refresh_app_store()", normal_apply), schedule.index("validation_dialog.destroy()", normal_apply))
        self.assertLess(
            schedule.index("validation_dialog.destroy()"),
            schedule.index('_show_copyable_error(Gtk, "KUHBS status refresh failed"'),
        )

    def test_startup_status_cache_uses_validated_definitions(self):
        main = nested_function_source("_run_gui")

        # The first status collection must receive the definitions validated at GUI startup
        self.assertIn(
            "local_definition_cache: list[dict] = list(validated.kuhb_definitions)",
            main,
        )

    def test_backup_umount_stays_available_for_partial_cleanup(self):
        backup_status = nested_function_source("backup_status")
        refresh_buttons = nested_function_source("refresh_buttons")

        self.assertIn("backup_storage_status", backup_status)
        self.assertIn("backup_mapper_active", backup_status)
        # A halted autostart backup VM disables every storage action until the operator starts it
        self.assertIn("backup_vm_running", backup_status)
        self.assertIn('all_button_map["backup-mount"].set_sensitive(not running and backup_vm_running and not cleanup_available)', refresh_buttons)
        self.assertIn('all_button_map["backup-umount"].set_sensitive(not running and backup_vm_running and cleanup_available)', refresh_buttons)

    def test_inaccessible_backup_media_is_not_reported_missing(self):
        backup_label = nested_function_source("_backup_label")
        list_color_class = nested_function_source("_list_color_class")

        self.assertIn('backup == "unavailable"', backup_label)
        self.assertIn('list_backup_label("unavailable")', backup_label)
        self.assertIn('"unavailable"', list_color_class)

    def test_selected_and_all_buttons_use_the_same_shared_action_plans_as_cli(self):
        action_plan = nested_function_source("gui_action_plan")
        page_builder = nested_function_source("make_kuhbs_page")
        refresh_buttons = nested_function_source("refresh_buttons")
        run_all_action = nested_function_source("run_all_action")

        self.assertIn("build_action_plan", action_plan)
        self.assertIn("build_all_action_plan", action_plan)
        self.assertIn("local_definition_cache", action_plan)
        self.assertIn("local_broken_cache", action_plan)
        self.assertNotIn("gui_action_allowed", GUI_PATH.read_text(encoding="utf-8"))
        self.assertIn("plan.can_run_exact", refresh_buttons)
        self.assertIn("gui_action_plan(action, selected_ids)", refresh_buttons)
        self.assertIn("gui_action_plan(action)", run_all_action)
        # Remove-All remains deliberately CLI-only instead of adding a destructive global GUI button
        self.assertNotIn('toolbar_button("Remove all")', page_builder)

    def test_rendering_and_action_plans_share_one_cached_lifecycle_snapshot(self):
        # Card rendering and every button computed during a repaint must see the same dom0 gate
        gui_state = nested_function_source("gui_state")
        action_plan = nested_function_source("gui_action_plan")
        startup_refresh = nested_function_source("refresh_status_cache")
        live_refresh = nested_function_source("schedule_status_refresh")

        self.assertIn("dom0_state_cache", gui_state)
        self.assertIn("status_by_kuhb", gui_state)
        self.assertIn('"dom0": dom0_state_cache', action_plan)
        self.assertNotIn("current_gate", action_plan)
        self.assertIn('dom0_state_cache = ctx.state_store.current_gate("dom0")', startup_refresh)
        self.assertIn('new_dom0_state = refresh_ctx.state_store.current_gate("dom0")', live_refresh)
        self.assertIn("dom0_state_cache = new_dom0_state", live_refresh)

    def test_terminal_completion_repaints_only_after_fresh_status_arrives(self):
        poll_operations = nested_function_source("poll_operations")
        actions_blocked = nested_function_source("actions_blocked")
        schedule_refresh = nested_function_source("schedule_status_refresh")
        apply_refresh = nested_function_source("apply")
        page_refreshes = [nested_function_source(name) for name in ("refresh_my", "refresh_system", "refresh_app_store")]
        error_refresh = apply_refresh[apply_refresh.index("if error is not None:"):apply_refresh.index("status_by_kuhb = new_status_by_kuhb")]
        success_refresh = apply_refresh[apply_refresh.index("ctx.defaults = refreshed.defaults"):]
        coalesced_refresh = success_refresh[:success_refresh.index("status_refresh_pending = False")]

        # Any user-driven repaint stays disabled until the callback installs post-operation truth
        self.assertIn("status_refresh_pending = True", poll_operations)
        self.assertIn("schedule_status_refresh()", poll_operations)
        self.assertNotIn("refresh_my()", poll_operations)
        self.assertNotIn("refresh_system()", poll_operations)
        self.assertNotIn("refresh_app_store()", poll_operations)
        self.assertIn("status_refresh_pending", actions_blocked)
        self.assertIn("status_refresh_running", actions_blocked)
        self.assertNotIn("validation_running", actions_blocked)
        for page_refresh in page_refreshes:
            self.assertIn("if status_refresh_pending", page_refresh)
        self.assertRegex(apply_refresh, r"nonlocal [^\n]*status_refresh_pending")
        self.assertIn("_validate_live_gui_config", schedule_refresh)
        self.assertIn("status_refresh_running or validation_running", schedule_refresh)
        self.assertLess(
            schedule_refresh.index("_validate_live_gui_config"),
            schedule_refresh.index("status_refresh_running = True"),
        )
        self.assertNotIn("if status_refresh_requested", error_refresh)
        self.assertIn("schedule_status_refresh()", error_refresh)
        self.assertIn("if status_refresh_requested", coalesced_refresh)
        self.assertIn("schedule_status_refresh()", coalesced_refresh)
        self.assertIn("return False", coalesced_refresh)
        self.assertLess(
            apply_refresh.index("ctx.defaults = refreshed.defaults"),
            apply_refresh.index("local_definition_cache = refresh_definitions"),
        )
        self.assertLess(
            apply_refresh.index("local_definition_cache = refresh_definitions"),
            apply_refresh.index("status_by_kuhb = new_status_by_kuhb"),
        )
        self.assertLess(apply_refresh.index("status_by_kuhb = new_status_by_kuhb"), apply_refresh.index("status_refresh_pending = False"))
        self.assertLess(apply_refresh.index("status_refresh_pending = False"), apply_refresh.index("refresh_my()"))

    def test_validation_error_reports_once_and_closes_the_gui(self):
        show_error = nested_function_source("_show_copyable_error")
        validate_gui = nested_function_source("_validate_gui_config")

        self.assertNotIn("Try Again", show_error)
        self.assertNotIn("retry", validate_gui)
        self.assertIn("inspect_startup_config(defaults_path, check_qubes=True)", validate_gui)
        self.assertIn("parent.destroy()", validate_gui)

    def test_editor_close_triggers_shared_validation_and_status_refresh(self):
        open_editor = nested_function_source("open_kuhb_editor")
        poll_editors = nested_function_source("poll_editors")

        self.assertIn('["gedit", "--standalone", "kuhb.yml"]', open_editor)
        self.assertIn("editor_processes.append(process)", open_editor)
        self.assertIn("GLib.timeout_add(250, poll_editors)", open_editor)
        self.assertIn("schedule_status_refresh()", poll_editors)
        self.assertNotIn("running_ops", poll_editors)

    def test_gui_owns_gui_marker_and_marks_its_cli_children(self):
        main = nested_function_source("main")
        start_terminal = nested_function_source("start_terminal_command")

        self.assertIn("CLI_MARKER.exists()", main)
        self.assertIn("GUI_MARKER.exists()", main)
        self.assertIn("GUI_MARKER.touch()", main)
        self.assertIn("GUI_MARKER.unlink(missing_ok=True)", main)
        self.assertIn(
            'command_parts = [wrapper, "env", "KUHBS_FROM_GUI=1", *command_args]',
            start_terminal,
        )

    def test_gui_refresh_contract_is_event_driven_and_documented(self):
        poll_operations = nested_function_source("poll_operations")
        poll_editors = nested_function_source("poll_editors")
        readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
        audit = (Path(__file__).parents[1] / "README-AUDIT.md").read_text(encoding="utf-8")

        self.assertIn("schedule_status_refresh()", poll_operations)
        self.assertIn("schedule_status_refresh()", poll_editors)
        contract = "Manual CLI commands abort while the GUI is open."
        safety = "Every action still starts a fresh CLI command that validates current configuration before side effects."
        self.assertIn(contract, readme)
        self.assertIn(safety, readme)
        self.assertIn(contract, audit)
        self.assertIn(safety, audit)

    def test_ctrl_a_stays_with_focused_text_entry(self):
        on_window_key_press = nested_function_source("on_window_key_press")

        editable_guard = "if isinstance(window.get_focus(), Gtk.Editable):"
        self.assertIn(editable_guard, on_window_key_press)
        self.assertLess(on_window_key_press.index(editable_guard), on_window_key_press.index("select_all_visible()"))

    def test_toolbar_buttons_wrap_when_the_window_narrows(self):
        toolbar_row = nested_function_source("toolbar_row")

        self.assertIn("Gtk.FlowBox()", toolbar_row)
        self.assertIn("left_toolbar.get_children()", toolbar_row)
        self.assertIn("right_toolbar.get_children()", toolbar_row)
        self.assertIn("flow.add(child)", toolbar_row)

    def test_system_select_all_stays_with_system_kuhbs(self):
        select_all_visible = nested_function_source("select_all_visible")

        self.assertIn("system_selected.update(visible_system_kuhb_ids)", select_all_visible)

    def test_qubes_os_vms_have_their_own_tab_selection_and_actions(self):
        source = GUI_PATH.read_text(encoding="utf-8")
        qubes_definitions = nested_function_source("_qubes_os_vm_definitions")
        select_all_visible = nested_function_source("select_all_visible")

        self.assertIn('Gtk.Label(label="Qubes OS VMs")', source)
        self.assertLess(source.index('Gtk.Label(label="System KUHBS")'), source.index('Gtk.Label(label="Qubes OS VMs")'))
        self.assertLess(source.index('Gtk.Label(label="Qubes OS VMs")'), source.index('Gtk.Label(label="Repos")'))
        self.assertIn("base_template_vm_names", qubes_definitions)
        self.assertIn('"id": "dom0"', qubes_definitions)
        self.assertIn("qubes_os_selected.update(visible_qubes_os_vm_ids)", select_all_visible)


    def test_dom0_health_uses_its_action_state_files(self):
        backup_label = nested_function_source("_backup_label")
        upgrade_label = nested_function_source("_upgrade_label")
        refresh_qubes_os = nested_function_source("refresh_qubes_os")

        self.assertIn('_dom0_health("backup", "present")', backup_label)
        self.assertIn('_dom0_health("upgrade", "upgraded")', upgrade_label)
        self.assertIn('show_health=definition["id"] == "dom0"', refresh_qubes_os)

    def test_qubes_os_page_omits_order_from_its_lifecycle_header(self):
        make_header_row = nested_function_source("make_header_row")
        refresh_qubes_os = nested_function_source("refresh_qubes_os")

        self.assertIn("show_order: bool = True", make_header_row)
        self.assertIn("qubes_os_list.pack_start(make_header_row(show_order=False), False, False, 1)", refresh_qubes_os)

    def test_template_rows_use_xml_status_color_and_keep_state_column_aligned(self):
        definitions = nested_function_source("_qubes_os_vm_definitions")
        refresh_qubes_os = nested_function_source("refresh_qubes_os")
        make_card = nested_function_source("make_card")
        status_block = nested_function_source("_status_block")

        self.assertIn("local_broken_cache", definitions)
        self.assertIn("qubes_vm_cache", definitions)
        self.assertIn('"Missing"', definitions)
        self.assertIn('"Wrong type"', definitions)
        self.assertIn('"present"', definitions)
        self.assertIn('status_text=definition["_status"]', refresh_qubes_os)
        self.assertIn('state_color=definition["_status_color"]', refresh_qubes_os)
        self.assertIn("show_order=False", refresh_qubes_os)
        self.assertIn("show_order: bool = True", make_card)
        self.assertIn("if show_order", status_block)
        for width in (104, 98):
            self.assertIn(f'_right_meta("", {width}', status_block)

    def test_repo_page_uses_one_status_header(self):
        make_header_row = nested_function_source("make_header_row")
        refresh_app_store = nested_function_source("refresh_app_store")

        self.assertIn('repository: bool = False', make_header_row)
        self.assertIn('_right_header("Status", 116, 12)', make_header_row)
        self.assertIn('make_header_row(repository=True)', refresh_app_store)

    def test_broken_card_identity_uses_active_name_and_complete_copyable_error(self):
        path = Path("/active/directory-name/kuhb.yml")
        issues = (ConfigIssue(path, "id is required"), ConfigIssue(path, "type is invalid"))
        entry = BrokenKuhb("directory-name", path, issues, {"id": "wrong", "name": "Raw name", "order": 200})

        card = _broken_card_definition(entry)

        self.assertEqual(card["id"], "directory-name")
        self.assertEqual(card["name"], "Raw name")
        self.assertTrue(card["_broken"])
        self.assertIn("id is required", card["description"])
        self.assertIn("type is invalid", card["description"])
        for invalid_order in (0, 1001, True):
            entry = BrokenKuhb("directory-name", path, issues, {"order": invalid_order})
            self.assertEqual(_broken_card_definition(entry)["order"], 1000)

    def test_broken_fingerprint_is_stable_changes_and_clears_for_recurrence(self):
        path = Path("/active/bad/kuhb.yml")
        first = (BrokenKuhb("bad", path, (ConfigIssue(path, "first"),)),)
        same = (BrokenKuhb("bad", path, (ConfigIssue(path, "first"),)),)
        changed = (BrokenKuhb("bad", path, (ConfigIssue(path, "second"),)),)

        self.assertEqual(_broken_fingerprint(first), _broken_fingerprint(same))
        self.assertNotEqual(_broken_fingerprint(first), _broken_fingerprint(changed))
        self.assertEqual(_broken_fingerprint(()), ())

    def test_broken_alerts_do_not_repeat_unchanged_entries_when_another_is_fixed(self):
        alpha_path = Path("/active/alpha/kuhb.yml")
        bravo_path = Path("/active/bravo/kuhb.yml")
        alpha = BrokenKuhb("alpha", alpha_path, (ConfigIssue(alpha_path, "bad alpha"),))
        bravo = BrokenKuhb("bravo", bravo_path, (ConfigIssue(bravo_path, "bad bravo"),))
        shown = set()

        self.assertEqual(_new_broken_entries((alpha, bravo), shown), [alpha, bravo])
        duplicate_shown = set()
        self.assertEqual(_new_broken_entries((alpha, alpha), duplicate_shown), [alpha])
        self.assertEqual(_new_broken_entries((bravo,), shown), [])
        self.assertEqual(_new_broken_entries((), shown), [])
        self.assertEqual(_new_broken_entries((bravo,), shown), [bravo])

    def test_broken_kuhb_enters_edit_only_repair_mode(self):
        repair_mode = nested_function_source("repair_mode")
        edit_allowed = nested_function_source("edit_allowed")
        repo_edit_allowed = nested_function_source("repo_edit_allowed")
        refresh_buttons = nested_function_source("refresh_buttons")
        refresh_list = nested_function_source("refresh_kuhb_list")
        refresh_repo = nested_function_source("refresh_app_store")
        start_command = nested_function_source("start_terminal_command")

        self.assertIn("local_broken_cache", repair_mode)
        self.assertIn("repo_broken_cache", repair_mode)
        self.assertIn("broken.active_id", edit_allowed)
        self.assertIn("broken.path", repo_edit_allowed)
        self.assertIn("repair_mode()", refresh_buttons)
        self.assertIn("edit_allowed(selected_set)", refresh_list)
        self.assertIn("repo_edit_allowed()", refresh_repo)
        self.assertIn("was_repair_mode", refresh_repo)
        self.assertIn("if repair_mode() != was_repair_mode", refresh_repo)
        self.assertIn("repair_mode()", start_command)

    def test_broken_card_is_red_generic_editable_and_has_no_health_actions(self):
        make_card = nested_function_source("make_card")
        status_block = nested_function_source("_status_block")
        kuhb_icon = nested_function_source("_kuhb_icon")
        refresh_list = nested_function_source("refresh_kuhb_list")
        displayed_definitions = nested_function_source("_kuhb_definitions")

        self.assertIn("BROKEN !!!", make_card)
        self.assertIn("kuhbs-list-red", status_block)
        self.assertIn("set_selectable", make_card)
        self.assertIn('definition.get("_broken")', kuhb_icon)
        self.assertIn("local_broken_cache", displayed_definitions)
        self.assertIn("edit_button.set_sensitive", refresh_list)

    def test_changed_broken_errors_alert_with_ok_only_and_fixed_recurrence_alerts_again(self):
        show_error = nested_function_source("_show_copyable_error")
        alert = nested_function_source("alert_broken_changes")
        refresh_app_store = nested_function_source("refresh_app_store")

        self.assertIn('dialog.add_button("OK", Gtk.ResponseType.OK)', show_error)
        self.assertIn("shown_broken_fingerprints", alert)
        self.assertIn("_new_broken_entries", alert)
        self.assertIn("Edit the broken KUHB", alert)
        self.assertIn("alert_broken_changes((*local_broken_cache, *repo_broken_cache))", refresh_app_store)

    def test_repo_link_and_unlink_launch_one_cli_batch(self):
        run_link = nested_function_source("run_repo_link")
        run_unlink = nested_function_source("run_repo_unlink")
        refresh_app_store = nested_function_source("refresh_app_store")

        self.assertIn('start_kuhbs_command("link", *link_sources, track=True)', run_link)
        self.assertIn(
            'start_kuhbs_command("unlink", *unlink_sources, affected=affected_ids, track=True)',
            run_unlink,
        )
        self.assertIn('action_allowed(gui_state(definition["id"]), "unlink")', refresh_app_store)
        self.assertNotIn("state_store.can_i", refresh_app_store)

    def test_repo_candidates_share_validator_and_broken_candidate_cannot_link(self):
        refresh_app_store = nested_function_source("refresh_app_store")

        self.assertIn("validate_kuhb_file", refresh_app_store)
        self.assertIn("qubes_vms=qubes_vm_cache", refresh_app_store)
        self.assertIn("validate_definition_set", refresh_app_store)
        self.assertIn("_broken_card_definition", refresh_app_store)
        self.assertIn("repo_linkable_sources", refresh_app_store)


if __name__ == "__main__":
    unittest.main()
