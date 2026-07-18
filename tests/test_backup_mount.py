# Purpose: Regression tests for KUHBS backup storage mount UX
# Scope: Fake qvm-run calls only; tests never touch real block devices
from __future__ import annotations

from contextlib import redirect_stdout
from pathlib import Path
import io
import tempfile
import unittest
from unittest.mock import patch

from kuhbs.command import CommandResult
from helpers import MappingRunner, RecordingEventLogger as EventLogger, RunnerContext
from kuhbs.operations import archive_storage, backup_mount
from helpers import current_defaults


class FakeGuest:
    # Store fake guest command results so backup-mount can be tested without Qubes
    def __init__(self, *, mountpoint_status: int = 1, ready_status: int = 1, mapper_status: int = 1):
        self.mountpoint_status = mountpoint_status
        self.ready_status = ready_status
        self.mapper_status = mapper_status
        self.commands: list[tuple[str, list[str], str | None]] = []

    def mark_ready(self) -> None:
        self.mountpoint_status = 0
        self.ready_status = 0

    def run(self, runner, kuh_name, command, *, user=None, check=True, log=True):
        self.commands.append((kuh_name, list(command), user))
        if command == ["/usr/bin/mountpoint", "--quiet", "/mnt"]:
            return CommandResult(self.mountpoint_status)
        if command == ["/usr/bin/test", "-d", "/mnt/kuhbs-backup"]:
            return CommandResult(self.ready_status)
        if command == ["/usr/bin/test", "-e", "/dev/mapper/kuhbs-backups"]:
            return CommandResult(self.mapper_status)
        return CommandResult(0)


class BackupMountTests(unittest.TestCase):
    def test_archive_root_matches_installed_qrexec_services(self):
        root = Path(__file__).resolve().parents[1]
        rpc_root = (
            root
            / "install/templates/usr/share/kuhbs/setup-scripts/kuhbs/setup/templates"
            / "etc/qubes-rpc"
        )

        for service in ("kuhbs.BackupRead", "kuhbs.BackupWrite"):
            text = (rpc_root / service).read_text(encoding="utf-8")
            self.assertIn(archive_storage.BACKUP_PATH, text)

    def test_backup_disk_mounts_use_storage_only_options(self):
        # Every mount path needs the same restrictions so initial format and later reopens cannot drift
        root = Path(__file__).resolve().parents[1]
        script = root / "install/templates/usr/share/kuhbs/setup-scripts/kuhbs/backup-mount.sh"
        mount_commands = [
            line.strip()
            for line in script.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("/usr/bin/mount --verbose")
        ]
        expected = "/usr/bin/mount --verbose --options nodev,nosuid,noexec,noatime,errors=remount-ro \"/dev/mapper/$crypt_name\" \"$mount_path\""

        self.assertEqual(mount_commands, [expected, expected, expected])

    def context(self, root: Path) -> RunnerContext:
        defaults = {
            "paths": {"config": str(root), "kuhbs": str(root / "my-kuhbs"), "setup_scripts": str(root / "setup-scripts")},
            "backup": {
                "kuh": "ndp-kuhbs-usb",
                "crypt_name": "kuhbs-backups",
            },
            "terminal": {"path": "/usr/bin/xfce4-terminal", "args": [], "fallback": {"path": "/usr/bin/xterm", "args": []}},
        }
        script = root / "setup-scripts" / "kuhbs" / "backup-mount.sh"
        script.parent.mkdir(parents=True)
        script.write_text("backup-mount helper")
        logger = EventLogger(root / "kuhbs.log", stdout=False)
        # Backup tests inject a command map so passive running checks never reach real Qubes tools
        return RunnerContext(current_defaults(defaults), logger, MappingRunner(logger))

    def run_mount(self, fake: FakeGuest, *, terminal_error: RuntimeError | None = None):
        output = io.StringIO()
        terminal_calls = []

        def fake_terminal(ctx, kuhb_id, kuh_name, script, *, user="root", script_args=None):
            terminal_calls.append((kuhb_id, kuh_name, Path(script), user, script_args))
            if terminal_error:
                raise terminal_error
            fake.mark_ready()

        with tempfile.TemporaryDirectory() as td:
            ctx = self.context(Path(td))
            # Guest command outcomes are isolated from the injected passive running-state check
            with patch.object(archive_storage, "run_in_kuh", side_effect=fake.run), \
                    patch.object(backup_mount, "run_in_kuh", side_effect=fake.run), \
                    patch.object(backup_mount, "run_script_in_kuh_terminal", side_effect=fake_terminal), \
                    redirect_stdout(output):
                backup_mount.run_mount(ctx)
        return output.getvalue(), terminal_calls, fake.commands

    def test_mount_halted_backup_vm_fails_without_terminal(self):
        # Mount must not use its terminal helper as an implicit VM start mechanism
        with tempfile.TemporaryDirectory() as td:
            ctx = self.context(Path(td))
            ctx.runner.returncodes[("qvm-check", "--quiet", "--running", "ndp-kuhbs-usb")] = 1
            with patch.object(backup_mount, "run_script_in_kuh_terminal") as terminal:
                with self.assertRaisesRegex(RuntimeError, "Backup VM ndp-kuhbs-usb is not running; start it first"):
                    backup_mount.run_mount(ctx)

        terminal.assert_not_called()

    def test_mount_runs_installed_backup_mount_script_in_root_terminal(self):
        fake = FakeGuest()

        output, terminal_calls, commands = self.run_mount(fake)

        self.assertIn("Opening a terminal as root@ndp-kuhbs-usb to mount KUHBS backup storage", output)
        self.assertIn("Storage device has been mounted to /mnt/kuhbs-backup", output)
        kuhb_id, kuh_name, script, user, script_args = terminal_calls[0]
        self.assertEqual(kuhb_id, "kuhbs")
        self.assertEqual(kuh_name, "ndp-kuhbs-usb")
        self.assertEqual(script.name, "backup-mount.sh")
        self.assertEqual(user, "root")
        self.assertEqual(script_args, ["/mnt", "/mnt/kuhbs-backup", "kuhbs-backups"])
        self.assertEqual(commands.count(("ndp-kuhbs-usb", ["/usr/bin/mountpoint", "--quiet", "/mnt"], "root")), 1)
        self.assertEqual(commands.count(("ndp-kuhbs-usb", ["/usr/bin/test", "-d", "/mnt/kuhbs-backup"], "root")), 0)
        self.assertEqual(commands.count(("ndp-kuhbs-usb", ["/usr/bin/test", "-e", "/dev/mapper/kuhbs-backups"], "root")), 1)

    def test_mount_terminal_error_aborts(self):
        fake = FakeGuest()

        with self.assertRaisesRegex(RuntimeError, "script failed"):
            self.run_mount(fake, terminal_error=RuntimeError("script failed"))
        self.assertEqual(fake.commands, [
            ("ndp-kuhbs-usb", ["/usr/bin/mountpoint", "--quiet", "/mnt"], "root"),
            ("ndp-kuhbs-usb", ["/usr/bin/test", "-e", "/dev/mapper/kuhbs-backups"], "root"),
        ])

    def test_halted_backup_vm_blocks_storage_probes(self):
        # Storage commands must report the exceptional VM state without qvm-run starting it
        with tempfile.TemporaryDirectory() as td:
            ctx = self.context(Path(td))
            ctx.runner.returncodes[("qvm-check", "--quiet", "--running", "ndp-kuhbs-usb")] = 1
            with patch.object(archive_storage, "run_in_kuh") as run_in_kuh:
                self.assertEqual(
                    archive_storage.backup_storage_status(ctx, "ndp-kuhbs-usb"),
                    (False, False),
                )
                with self.assertRaisesRegex(RuntimeError, "Backup VM ndp-kuhbs-usb is not running; start it first"):
                    archive_storage.require_backup_vm_running(ctx, "ndp-kuhbs-usb")
                with self.assertRaisesRegex(RuntimeError, "Backup VM ndp-kuhbs-usb is not running; start it first"):
                    archive_storage.require_backup_storage(ctx, "ndp-kuhbs-usb")

        run_in_kuh.assert_not_called()

    def test_storage_readiness_stops_after_missing_mountpoint(self):
        fake = FakeGuest(mountpoint_status=1, ready_status=0)
        with tempfile.TemporaryDirectory() as td:
            ctx = self.context(Path(td))
            # This test isolates mountpoint short-circuiting after the injected running gate
            with patch.object(archive_storage, "run_in_kuh", side_effect=fake.run):
                self.assertFalse(archive_storage.backup_storage_ready(ctx, "ndp-kuhbs-usb"))

        self.assertEqual(fake.commands, [
            ("ndp-kuhbs-usb", ["/usr/bin/mountpoint", "--quiet", "/mnt"], "root"),
        ])

    def test_storage_status_reports_partial_mount_for_gui_cleanup(self):
        fake = FakeGuest(mountpoint_status=0, ready_status=1)
        with tempfile.TemporaryDirectory() as td:
            ctx = self.context(Path(td))
            # This test isolates partial-mount reporting after the injected running gate
            with patch.object(archive_storage, "run_in_kuh", side_effect=fake.run):
                ready, mounted = archive_storage.backup_storage_status(ctx, "ndp-kuhbs-usb")

        self.assertFalse(ready)
        self.assertTrue(mounted)

    def test_mount_refuses_to_run_helper_on_partial_mount(self):
        fake = FakeGuest(mountpoint_status=0, ready_status=1)

        with self.assertRaisesRegex(RuntimeError, "archive directory .* is missing; run backup-umount"):
            self.run_mount(fake)

        self.assertEqual(fake.commands, [
            ("ndp-kuhbs-usb", ["/usr/bin/mountpoint", "--quiet", "/mnt"], "root"),
            ("ndp-kuhbs-usb", ["/usr/bin/test", "-d", "/mnt/kuhbs-backup"], "root"),
        ])

    def test_mount_refuses_to_run_helper_with_stale_mapper(self):
        fake = FakeGuest(mapper_status=0)

        with self.assertRaisesRegex(RuntimeError, "mapper kuhbs-backups is already active.*run backup-umount"):
            self.run_mount(fake)

        self.assertEqual(fake.commands, [
            ("ndp-kuhbs-usb", ["/usr/bin/mountpoint", "--quiet", "/mnt"], "root"),
            ("ndp-kuhbs-usb", ["/usr/bin/test", "-e", "/dev/mapper/kuhbs-backups"], "root"),
        ])

    def test_umount_halted_backup_vm_fails_without_guest_probe(self):
        # Every storage action shares the explicit-start prerequisite instead of silently changing VM state
        with tempfile.TemporaryDirectory() as td:
            ctx = self.context(Path(td))
            ctx.runner.returncodes[("qvm-check", "--quiet", "--running", "ndp-kuhbs-usb")] = 1
            with patch.object(backup_mount, "run_in_kuh") as run_in_kuh:
                with self.assertRaisesRegex(RuntimeError, "Backup VM ndp-kuhbs-usb is not running; start it first"):
                    backup_mount.run_unmount(ctx)

        run_in_kuh.assert_not_called()

    def test_umount_checks_mount_once_then_closes_mapper(self):
        fake = FakeGuest(mountpoint_status=0, ready_status=0, mapper_status=0)
        with tempfile.TemporaryDirectory() as td:
            ctx = self.context(Path(td))
            output = io.StringIO()
            # A running backup VM reaches the existing mount and mapper cleanup path
            with patch.object(archive_storage, "run_in_kuh", side_effect=fake.run), \
                    patch.object(backup_mount, "run_in_kuh", side_effect=fake.run), \
                    redirect_stdout(output):
                backup_mount.run_unmount(ctx)

        self.assertEqual(fake.commands, [
            ("ndp-kuhbs-usb", ["/usr/bin/mountpoint", "--quiet", "/mnt"], "root"),
            ("ndp-kuhbs-usb", ["/usr/bin/umount", "/mnt"], "root"),
            (
                "ndp-kuhbs-usb",
                ["/usr/bin/test", "-e", "/dev/mapper/kuhbs-backups"],
                "root",
            ),
            ("ndp-kuhbs-usb", ["/usr/sbin/cryptsetup", "close", "kuhbs-backups"], "root"),
        ])
        self.assertIn("Storage device has been unmounted", output.getvalue())

    def test_umount_external_mount_does_not_close_absent_mapper(self):
        fake = FakeGuest(mountpoint_status=0, ready_status=0, mapper_status=1)
        with tempfile.TemporaryDirectory() as td:
            ctx = self.context(Path(td))
            # External mounts still need guest cleanup while the backup VM is running
            with patch.object(archive_storage, "run_in_kuh", side_effect=fake.run), \
                    patch.object(backup_mount, "run_in_kuh", side_effect=fake.run):
                backup_mount.run_unmount(ctx)

        self.assertIn(
            ("ndp-kuhbs-usb", ["/usr/bin/umount", "/mnt"], "root"),
            fake.commands,
        )
        self.assertNotIn(
            (
                "ndp-kuhbs-usb",
                ["/usr/sbin/cryptsetup", "close", "kuhbs-backups"],
                "root",
            ),
            fake.commands,
        )


if __name__ == "__main__":
    unittest.main()
