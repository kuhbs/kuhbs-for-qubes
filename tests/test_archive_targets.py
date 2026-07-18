# Purpose: Regression tests for shared backup/restore archive target orchestration
# Scope: Fake jobs only; backup.py and restore.py own real archive behavior
from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from helpers import RecordingEventLogger as EventLogger
from kuhbs.model import merge_kuhb_definition, resolve_kuhs
from kuhbs.operations import OperationContext
from kuhbs.operations import archive_targets
from helpers import current_defaults


def definition(kuhb_id: str, instances: list[str] | None = None) -> dict:
    # Tests use sta instances because each instance maps to a concrete backupable VM
    return {
        "id": kuhb_id,
        "name": kuhb_id,
        "description": kuhb_id,
        "icon": "icon.svg",
        "order": 500,
        "type": "sta",
        "template": "debian-13-minimal",
        "kuhs": {
            "sta": {
                "instances": [
                    {"id": instance, "prefs": {"label": "green"}, "backup": {"paths": ["/home/user/work"]}}
                    for instance in (instances or ["default"])
                ]
            }
        },
    }


def enabled_kuhs(ctx: OperationContext, item: dict):
    # Match backup policy shape: only kuhs with backup paths become archive targets
    merged = merge_kuhb_definition(ctx.defaults, item)
    return [kuh for kuh in resolve_kuhs(merged) if "backup" in kuh.config]


class ArchiveTargetTests(unittest.TestCase):
    def context(self, root: Path, batch: int = 5) -> OperationContext:
        defaults = {
            "paths": {"config": str(root), "kuhbs": str(root / "my-kuhbs")},
            "batch_size": {"backup": batch},
        }
        return OperationContext(current_defaults(defaults), EventLogger(root / "kuhbs.log", stdout=False))

    def test_targets_for_all_sorts_kuhbs_and_keeps_kuh_order_inside_kuhb(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = self.context(Path(td))
            definitions = (definition("zulu", ["b", "a"]), definition("alpha", ["b", "a"]))

            targets = archive_targets.targets_for_all(ctx, definitions, enabled_kuhs, include_dom0=True)

            self.assertEqual([target.id for target in targets], ["sta-alpha-b", "sta-alpha-a", "sta-zulu-b", "sta-zulu-a", "dom0"])


    def test_temporary_qrexec_policy_removes_policy_when_install_is_interrupted(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = self.context(Path(td))
            runner = Mock()
            runner.shell_for_dom0.side_effect = KeyboardInterrupt
            ctx._runner = runner
            policy_path = "/etc/qubes/policy.d/30-kuhbs-backup-write-app-signal.policy"

            with self.assertRaises(KeyboardInterrupt):
                with archive_targets.temporary_qrexec_policy(
                    ctx,
                    policy_path,
                    "kuhbs.BackupWrite + app-signal ndp-kuhbs-usb allow",
                ):
                    self.fail("interrupted policy installation must not yield")

            runner.run_for_dom0.assert_called_once_with(["sudo", "rm", "-f", policy_path])

    def test_disabled_archive_config_produces_no_archive_target(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = self.context(Path(td))
            data = definition("alpha")
            data["kuhs"]["sta"]["instances"][0].pop("backup")
            ctx.set_state("alpha", "create", "completed")
            targets = archive_targets.targets_for_all(ctx, (data,), enabled_kuhs, include_dom0=False)

            self.assertEqual(targets, [])
            self.assertIsNone(ctx.state_store.get_state("alpha", "backup"))

    def test_run_targets_respects_batch_size_limit(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = self.context(Path(td), batch=2)
            lock = threading.Lock()
            active = 0
            max_seen = 0

            def job(_target):
                nonlocal active, max_seen
                with lock:
                    active += 1
                    max_seen = max(max_seen, active)
                time.sleep(0.02)
                with lock:
                    active -= 1

            targets = [archive_targets.ArchiveTarget(f"app-{index}", f"kuhb-{index}", None, None) for index in range(5)]
            for target in targets:
                ctx.set_state(target.kuhb_id, "create", "completed")
            archive_targets.run_targets(ctx, targets, job, source="backup", action="backup", success_phrase="backed up", failure_phrase="back up")

            self.assertLessEqual(max_seen, 2)

    def test_run_targets_reports_success_and_failures_in_target_order(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = self.context(Path(td), batch=3)
            calls: list[str] = []

            def run_one(target):
                calls.append(target.id)
                if target.id in {"alpha", "dom0"}:
                    raise RuntimeError(f"{target.id} boom")

            targets = [
                archive_targets.ArchiveTarget("alpha", "alpha", None, None),
                archive_targets.ArchiveTarget("bravo", "bravo", None, None),
                archive_targets.Dom0ArchiveTarget(),
            ]
            ctx.set_state("alpha", "create", "completed")
            ctx.set_state("bravo", "create", "completed")

            with self.assertRaisesRegex(RuntimeError, "backup failed"):
                archive_targets.run_targets(ctx, targets, run_one, source="backup", action="backup", success_phrase="backed up", failure_phrase="back up")

            self.assertEqual(set(calls), {"alpha", "bravo", "dom0"})
            self.assertEqual(ctx.state_store.get_state("dom0", "backup"), "failed")
            log = (Path(td) / "kuhbs.log").read_text(encoding="utf-8")
            self.assertIn("Successfully backed up kuhs: bravo", log)
            self.assertIn("Failed to back up kuhs: alpha, dom0", log)


if __name__ == "__main__":
    unittest.main()

