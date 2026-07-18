# Purpose: Regression tests for dom0 backup/restore command construction
# Scope: Explicit fake runners only; never touches real Qubes state
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kuhbs.command import CommandResult, CommandRunner
from helpers import RecordingEventLogger as EventLogger
from kuhbs.operations import OperationContext
from kuhbs.operations.archive_targets import ArchiveTarget, Dom0ArchiveTarget
from kuhbs.operations import backup, restore
from helpers import MappingRunner, RunnerContext, current_defaults


def context(tmp_path: Path) -> OperationContext:
    defaults = {
        "paths": {
            "config": str(tmp_path / ".kuhbs"),
            "kuhbs": str(tmp_path / ".kuhbs/my-kuhbs"),
            "scripts": str(tmp_path / ".kuhbs/work/scripts"),
            "setup_scripts": str(tmp_path / "usr/share/kuhbs/setup-scripts"),
            "user_setup_scripts": str(tmp_path / ".kuhbs/setup-scripts"),
            "desktop_applications": str(tmp_path / ".local/share/applications"),
        },
        "backup": {
            "kuh": "ndp-kuhbs-usb",
            "crypt_name": "kuhbs-backups",
            "ignore_failed_read": True,
            "dom0_ignore_failed_read": True,
            "dom0_paths": ["/home/user/*"],
        },
        "logging": {"colors": {}},
    }
    setup_scripts = tmp_path / "usr/share/kuhbs/setup-scripts/kuhbs"
    setup_scripts.mkdir(parents=True)
    # Fake-runner tests still mirror the installed setup-script tree because production has no repo fallback
    (setup_scripts / "kuhbs-run-script.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    logger = EventLogger(log_path=tmp_path / "kuhbs.log", color=False, stdout=False)
    return RunnerContext(current_defaults(defaults), logger, MappingRunner(logger))


class FailingDom0ExtractRunner(CommandRunner):
    def __init__(self, logger):
        super().__init__(logger)
        self.commands: list[list[str]] = []

    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        self.commands.append(list(args))
        command = " ".join(args)
        returncode = 1 if "tar -I zstd -xpf - -C /" in command else 0
        if check and returncode != 0:
            raise RuntimeError("dom0 tar failed")
        return CommandResult(returncode=returncode)

class Dom0BackupRestoreTests(unittest.TestCase):
    def test_backup_runs_dom0_after_the_vm_worker_pool(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = context(Path(td))
            vm_target = ArchiveTarget("app-alpha", "alpha", {"id": "alpha"}, Mock(name="app-alpha"))
            dom0_target = Dom0ArchiveTarget()
            events: list[str] = []

            def fake_pool(_ctx, targets, _run_one, **_kwargs):
                self.assertEqual([target.id for target in targets], ["app-alpha"])
                events.append("vm-pool")

            with patch.object(backup, "_preflight_backup_targets", return_value={"app-alpha": True}), \
                    patch.object(backup, "run_targets", side_effect=fake_pool), \
                    patch.object(backup, "run_dom0", side_effect=lambda _ctx: events.append("dom0")):
                backup.run_archive_targets(ctx, [vm_target, dom0_target])

            self.assertEqual(events, ["vm-pool", "dom0"])

    def test_dom0_backup_streams_to_backup_kuh(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)

            backup.run_dom0(ctx)

            log_text = (tmp_path / "kuhbs.log").read_text(encoding="utf-8")
            self.assertIn("zstd -T0 -3", log_text)
            self.assertIn("-cpf - --ignore-failed-read -- /home/user/*", log_text)
            self.assertIn("qvm-run --pass-io --user root ndp-kuhbs-usb", log_text)
            self.assertIn("cat - > /mnt/kuhbs-backup/dom0.tar.zst", log_text)
            self.assertNotIn("test ! -L /mnt/kuhbs-backup/dom0.tar.zst", log_text)

    def test_dom0_backup_does_not_create_config_file(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)

            backup.run_dom0(ctx)

            self.assertFalse((tmp_path / ".kuhbs/config/backup-dom0.yml").exists())

    def test_dom0_backup_can_keep_strict_tar_read_errors(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            ctx.defaults["backup"]["dom0_ignore_failed_read"] = False

            backup.run_dom0(ctx)

            log_text = (tmp_path / "kuhbs.log").read_text(encoding="utf-8")
            self.assertIn("zstd -T0 -3", log_text)
            self.assertIn("-cpf - -- /home/user/*", log_text)
            self.assertNotIn("--ignore-failed-read", log_text)

    def test_dom0_restore_checks_and_streams_archive_from_backup_kuh(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)

            restore.run_archive_targets(ctx, [Dom0ArchiveTarget()])

            log_text = (tmp_path / "kuhbs.log").read_text(encoding="utf-8")
            self.assertIn("test -f /mnt/kuhbs-backup/dom0.tar.zst", log_text)
            self.assertIn("/usr/bin/mountpoint --quiet /mnt", log_text)
            self.assertNotIn("test ! -L /mnt/kuhbs-backup/dom0.tar.zst", log_text)
            backup_commands = [line for line in log_text.splitlines() if "COMMAND   ndp-kuhbs-usb" in line]
            # One passive qvm-check now enforces the explicit-running backup VM prerequisite
            self.assertEqual(len(backup_commands), 5)
            self.assertIn("qvm-run --pass-io --user root ndp-kuhbs-usb", log_text)
            self.assertIn("cat /mnt/kuhbs-backup/dom0.tar.zst", log_text)
            self.assertIn("tar -I zstd -xpf - -C /", log_text)
            self.assertNotIn("qvm-copy-to-vm dom0", log_text)

    def test_restore_archive_targets_runs_dom0_after_vm_targets(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            ctx.set_state("alpha", "create", "completed")
            ctx.set_state("dom0", "backup", "completed")
            calls: list[str] = []
            targets = [
                ArchiveTarget("app-alpha", "alpha", {"id": "alpha"}, SimpleNamespace(name="app-alpha")),
                Dom0ArchiveTarget(),
            ]

            def fake_run_archive_target(_ctx, target):
                calls.append(target.id)

            def fake_run_dom0(_ctx):
                calls.append("dom0")

            # Ordering is isolated from the separate live target power-state contract
            with patch("kuhbs.operations.restore.run_archive_target", side_effect=fake_run_archive_target), \
                 patch("kuhbs.operations.restore.run_dom0", side_effect=fake_run_dom0), \
                 patch("kuhbs.operations.restore.vm_power_state", return_value="halted"):
                restore.run_archive_targets(ctx, targets, source="restore-all")

            self.assertEqual(calls, ["app-alpha", "dom0"])
            self.assertEqual(ctx.state_store.get_state("dom0", "restore"), "completed")

    def test_dom0_restore_propagates_stream_extract_failure(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base = context(tmp_path)
            runner = FailingDom0ExtractRunner(base.logger)
            ctx = RunnerContext(base.defaults, base.logger, runner)

            with self.assertRaisesRegex(RuntimeError, "dom0 tar failed"):
                restore.run_archive_targets(ctx, [Dom0ArchiveTarget()])

            commands = [" ".join(command) for command in runner.commands]
            self.assertTrue(any("qvm-run --pass-io --user root ndp-kuhbs-usb" in command and "tar -I zstd -xpf - -C /" in command for command in commands))


if __name__ == "__main__":
    unittest.main()
