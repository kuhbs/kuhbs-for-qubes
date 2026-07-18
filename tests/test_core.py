from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import Mock
import yaml

from kuhbs.config import load_defaults, resolve_path
from helpers import RecordingEventLogger as EventLogger
from kuhbs.operations import OperationContext
from kuhbs.operations.planning import Plan, build_action_plan, build_all_action_plan, build_upgrade_plan
from kuhbs.validation import BrokenKuhb, ConfigIssue, validate_startup_config
from tests.test_validation import valid_definition
from kuhbs.model import action_allowed, base_template_vm_names, merge_kuhb_definition, resolve_kuhs
from kuhbs.state import StateStore


ROOT = Path(__file__).resolve().parents[1]


def defaults():
    return load_defaults(ROOT / "defaults.yml")

def validate_modified_defaults(data: dict):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "defaults.yml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return validate_startup_config(path)


class CoreTests(unittest.TestCase):
    def test_base_template_vm_names_are_sorted_and_unique(self):
        definitions = (
            {"template": "whonix-workstation-18"},
            {"template": "debian-13-minimal"},
            {"template": "debian-13-minimal"},
        )

        self.assertEqual(
            base_template_vm_names(definitions),
            ("debian-13-minimal", "whonix-workstation-18"),
        )

    def test_explicit_upgrade_plan_separates_kuhbs_dom0_and_base_templates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = defaults()
            data["paths"]["config"] = str(root / "config")
            data["paths"]["kuhbs"] = str(root / "config/my-kuhbs")
            ctx = OperationContext(data, EventLogger(log_path=root / "kuhbs.log", color=False))
            definition = merge_kuhb_definition(data, valid_definition("signal"))
            ctx.state_store.set_state("signal", "create", "completed")

            plan = build_upgrade_plan(
                ctx,
                (definition,),
                ["debian-13-minimal", "dom0", "signal", "debian-13-minimal"],
            )

            self.assertEqual([item["id"] for item in plan.runnable], ["signal", "dom0"])
            self.assertEqual(plan.qubes_templates, ("debian-13-minimal",))
            self.assertEqual(plan.skipped, ())

    def test_broken_id_collision_does_not_resolve_as_a_base_template(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = defaults()
            data["paths"]["config"] = str(root / "config")
            data["paths"]["kuhbs"] = str(root / "config/my-kuhbs")
            ctx = OperationContext(data, EventLogger(log_path=root / "kuhbs.log", color=False))
            definition = merge_kuhb_definition(data, valid_definition("debian-13-minimal"))

            plan = build_upgrade_plan(
                ctx,
                (definition,),
                ["debian-13-minimal"],
                broken_reasons={"debian-13-minimal": "reserved base TemplateVM name"},
            )

            self.assertEqual(plan.qubes_templates, ())
            self.assertEqual(
                plan.skipped,
                (("debian-13-minimal", "configuration broken: reserved base TemplateVM name"),),
            )

    def test_state_change_is_logged_to_cli_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ctx = OperationContext(
                {"paths": {"config": str(root)}},
                EventLogger(log_path=root / "kuhbs.log", color=False),
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                ctx.set_state("signal", "create", "start")

            log = (root / "kuhbs.log").read_text(encoding="utf-8")
            self.assertIn("STATUS", log)
            self.assertIn("State changed: create:start", log)
            self.assertIn("State changed: create:start", stdout.getvalue())


    def test_load_defaults_requires_existing_yaml(self):
        data = defaults()

        self.assertEqual(data["schema"], 1)
        self.assertEqual(data["backup"]["kuh"], "ndp-kuhbs-usb")
        self.assertEqual(data["upgrade"]["max_age_minutes"], 720)
        self.assertIn("ndp-kuhbs-net-cache", data["upgrade"]["restart_without_prompt"])
        self.assertEqual(data["terminal"]["path"], "/usr/bin/xfce4-terminal")
        self.assertIn("default_kuhb", data)
        self.assertIn("default_launcher", data)

    def test_resolve_path_expands_tilde_only(self):
        self.assertEqual(resolve_path("~/.kuhbs/state"), Path.home() / ".kuhbs/state")

    def test_startup_validation_requires_backup_runtime_keys(self):
        for key in ("kuh", "crypt_name"):
            data = deepcopy(defaults())
            data["backup"].pop(key)

            with self.assertRaisesRegex(ValueError, f"backup.{key}"):
                validate_modified_defaults(data)

    def test_startup_validation_rejects_empty_backup_runtime_strings(self):
        for key in ("kuh", "crypt_name"):
            data = deepcopy(defaults())
            data["backup"][key] = ""

            with self.assertRaisesRegex(ValueError, f"backup.{key} must be a non-empty string"):
                validate_modified_defaults(data)

    def test_startup_validation_rejects_non_bool_backup_flags(self):
        for key in ("ignore_failed_read", "dom0_ignore_failed_read"):
            data = deepcopy(defaults())
            data["backup"][key] = "True"

            with self.subTest(key=key), self.assertRaisesRegex(
                ValueError,
                f"backup.{key} must be True or False",
            ):
                validate_modified_defaults(data)

    def test_startup_validation_rejects_malformed_dom0_paths(self):
        data = deepcopy(defaults())
        data["backup"]["dom0_paths"] = "not-a-list"

        with self.assertRaisesRegex(ValueError, "backup.dom0_paths must be a list of non-empty strings"):
            validate_modified_defaults(data)

    def test_startup_validation_rejects_bool_batch_size_backup(self):
        data = deepcopy(defaults())
        data["batch_size"]["backup"] = True

        with self.assertRaisesRegex(ValueError, "batch_size.backup must be a positive integer"):
            validate_modified_defaults(data)

    def test_action_matrix_for_single_states(self):
        self.assertIs(action_allowed("linked", "create"), True)
        self.assertIs(action_allowed("linked", "backup"), False)
        self.assertIs(action_allowed("create:completed", "backup"), True)
        self.assertIs(action_allowed("backup:failed", "backup"), True)
        self.assertIs(action_allowed("restore:failed", "restore"), True)
        self.assertIs(action_allowed("backup:start", "remove"), False)
        self.assertIs(action_allowed("dom0", "backup"), True)
        # Fresh dom0 can upgrade, but a failed Restore must retain its repair-only gate
        self.assertIs(action_allowed("dom0", "upgrade"), True)
        self.assertIs(action_allowed("restore:failed", "upgrade"), False)
        self.assertIs(action_allowed("dom0", "restore"), False)


    def test_state_store_reads_and_writes_plain_state_words(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            store = StateStore(tmp_path)

            self.assertEqual(store.current_gate("signal"), "linked")
            store.set_state("signal", "create", "completed")
            self.assertEqual((tmp_path / "signal/create").read_text(), "completed\n")
            self.assertEqual(store.get_state("signal", "create"), "completed")
            self.assertEqual(store.current_gate("signal"), "create:completed")

            with self.assertRaises(ValueError):
                store.set_state("signal", "create", "nonsense")

    def test_dom0_state_allows_first_backup_but_requires_a_completed_backup_for_restore(self):
        # Dom0 always exists, but restore is offered only after state records a usable backup
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(td)

            self.assertEqual(store.current_gate("dom0"), "dom0")
            self.assertEqual(store.can_i("backup", "dom0", linked=True), (True, ""))
            self.assertEqual(
                store.can_i("restore", "dom0", linked=True),
                (False, "cannot restore dom0; state is dom0"),
            )

            store.set_state("dom0", "backup", "completed")

            self.assertEqual(store.can_i("restore", "dom0", linked=True), (True, ""))

    def test_dom0_upgrade_health_does_not_replace_the_lifecycle_gate(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(td)

            store.set_state("dom0", "upgrade", "completed")

            self.assertEqual(store.get_state("dom0", "upgrade"), "completed")
            self.assertEqual(store.current_gate("dom0"), "dom0")

    def test_state_store_uses_the_most_recent_action_file(self):
        # A newer interrupted create must override an older completed remove
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(td)
            store.set_state("element", "remove", "completed")
            store.set_state("element", "create", "failed")
            remove_path = store.action_path("element", "remove")
            create_path = store.action_path("element", "create")
            os.utime(remove_path, ns=(1, 1))
            os.utime(create_path, ns=(2, 2))

            self.assertEqual(store.current_gate("element"), "create:failed")

    def test_merge_kuhb_definition_inherits_defaults_without_mutating_input(self):
        data = {
            "id": "app-test",
            "name": "App Test",
            "description": "Test",
            "icon": "icon.svg",
            "order": 10,
            "type": "app",
            "template": "debian-13-minimal",
            "kuhs": {
                "app": {"instances": [{"id": "default", "prefs": {"label": "green"}, "services": {"custom": True}}]},
            },
        }

        merged = merge_kuhb_definition(defaults(), data)

        app = merged["kuhs"]["app"]["instances"][0]
        self.assertEqual(app["prefs"]["label"], "green")
        self.assertEqual(app["prefs"]["memory"], 4096)
        self.assertEqual(app["services"]["meminfo-writer"], False)
        self.assertEqual(app["services"]["custom"], True)
        self.assertNotIn("memory", data["kuhs"]["app"]["instances"][0]["prefs"])

    def test_resolve_kuhs_derives_names_without_udp_instances(self):
        definition = {
            "id": "test-ndp",
            "name": "Test Ndp",
            "description": "Test",
            "icon": "icon.svg",
            "order": 10,
            "template": "debian-13-minimal",
            "type": "ndp",
            "kuhs": {
                "app": {"instances": [{"id": "default"}]},
                "ndp": {"instances": [{"id": "default"}]},
            },
        }

        merged = merge_kuhb_definition(defaults(), definition)
        names = [kuh.name for kuh in resolve_kuhs(merged)]

        self.assertEqual(names, ["tpl-test-ndp", "app-test-ndp", "ndp-test-ndp"])

    def test_resolve_kuhs_accepts_list_instances_with_explicit_ids(self):
        definition = {
            "id": "signal2",
            "name": "Signal 2",
            "description": "Test",
            "icon": "icon.svg",
            "order": 10,
            "template": "debian-13-minimal",
            "type": "app",
            "kuhs": {
                "app": {
                    "instances": [
                        {"id": "work", "prefs": {"label": "green"}},
                        {"id": "private", "prefs": {"label": "yellow"}},
                    ],
                },
            },
        }

        merged = merge_kuhb_definition(defaults(), definition)
        kuhs = resolve_kuhs(merged)

        self.assertEqual([kuh.name for kuh in kuhs], ["tpl-signal2", "app-signal2-work", "app-signal2-private"])
        self.assertNotIn("template", kuhs[1].config)
        self.assertEqual(kuhs[1].config["prefs"]["label"], "green")
        self.assertEqual(kuhs[1].config["prefs"]["memory"], 4096)

    def test_shared_upgrade_all_plan_keeps_fresh_dom0_and_obeys_failed_restore_gate(self):
        # Dom0 must make an empty local plan runnable without bypassing later lifecycle failures
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = defaults()
            data["paths"]["config"] = str(root / "config")
            data["paths"]["kuhbs"] = str(root / "config/my-kuhbs")
            ctx = OperationContext(data, EventLogger(log_path=root / "kuhbs.log", color=False))

            plan = build_all_action_plan(ctx, (), (), "upgrade-all")
            self.assertEqual([item["id"] for item in plan.runnable], ["dom0"])

            ctx.state_store.set_state("dom0", "restore", "failed")
            plan = build_all_action_plan(ctx, (), (), "upgrade-all")
            self.assertEqual(plan.runnable, ())
            self.assertEqual(plan.skipped, (("dom0", "state is restore:failed"),))

    def test_shared_archive_all_plan_uses_current_config_and_dom0_rules(self):
        # Archive plans must share config participation without making Restore depend on dom0 paths
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = defaults()
            data["paths"]["config"] = str(root / "config")
            data["paths"]["kuhbs"] = str(root / "config/my-kuhbs")
            data["backup"]["dom0_paths"] = []
            ctx = OperationContext(data, EventLogger(log_path=root / "kuhbs.log", color=False))
            raw = valid_definition("signal")
            raw["kuhs"]["app"]["instances"][0]["backup"] = {"paths": ["/home/user/.config/Signal"]}
            definition = merge_kuhb_definition(data, raw)
            ctx.state_store.set_state("signal", "create", "completed")

            backup_plan = build_all_action_plan(ctx, (definition,), (), "backup-all")
            self.assertEqual([item["id"] for item in backup_plan.runnable], ["signal"])

            ctx.state_store.set_state("dom0", "backup", "completed")
            restore_plan = build_all_action_plan(ctx, (definition,), (), "restore-all")
            self.assertEqual([item["id"] for item in restore_plan.runnable], ["signal", "dom0"])

    def test_shared_all_plan_partitions_broken_and_runnable_targets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = defaults()
            data["paths"]["config"] = str(root / "config")
            data["paths"]["kuhbs"] = str(root / "config/my-kuhbs")
            ctx = OperationContext(data, EventLogger(log_path=root / "kuhbs.log", color=False))
            definition = merge_kuhb_definition(data, valid_definition("good"))
            issue = ConfigIssue(root / "bad/kuhb.yml", "id is required")
            broken = BrokenKuhb("bad", issue.path, (issue,))

            plan = build_all_action_plan(ctx, (definition,), (broken,), "create-all")

            self.assertEqual([item["id"] for item in plan.runnable], ["good"])
            self.assertIn(("bad", f"configuration broken: {issue.path}: id is required"), plan.skipped)

    def test_plan_exact_request_requires_every_target_to_be_runnable(self):
        self.assertTrue(Plan(({"id": "alpha"},), ()).can_run_exact)
        self.assertFalse(Plan(({"id": "alpha"},), (("bravo", "blocked"),)).can_run_exact)
        self.assertFalse(Plan((), ()).can_run_exact)

    def test_shared_action_plan_uses_gui_cached_or_cli_fresh_gates(self):
        definitions = ({"id": "alpha", "order": 10}, {"id": "bravo", "order": 20})
        ctx = Mock()
        ctx.state_store.current_gate.side_effect = AssertionError("cached GUI plan reread state")

        plan = build_action_plan(
            ctx,
            definitions,
            ["alpha", "bravo"],
            "create",
            states={"alpha": "linked", "bravo": "create:completed"},
        )

        self.assertEqual([item["id"] for item in plan.runnable], ["alpha"])
        self.assertEqual(plan.skipped, (("bravo", "state is create:completed"),))


if __name__ == "__main__":
    unittest.main()
