# Purpose: Regression tests for create restore
# Scope: Uses temp dirs and fake runners instead of mutating real Qubes state
from pathlib import Path
import tempfile
import unittest

from helpers import RecordingEventLogger as EventLogger
from kuhbs.model import merge_kuhb_definition
from kuhbs.operations import create
from helpers import MappingRunner, RunnerContext, current_defaults


def context(tmp_path):
    runner = tmp_path / "usr/share/kuhbs/setup-scripts/kuhbs/kuhbs-run-script.sh"
    runner.parent.mkdir(parents=True)
    # Runner fixture lives at the configured installed path; production has no repo fallback
    runner.write_text("#!/bin/bash\nexit 0\n")
    runner.chmod(0o700)
    defaults = {
        "paths": {
            "config": str(tmp_path / ".kuhbs"),
            "scripts": str(tmp_path / ".kuhbs/work/scripts"),
            "setup_scripts": str(tmp_path / "usr/share/kuhbs/setup-scripts"),
            "user_setup_scripts": str(tmp_path / ".kuhbs/setup-scripts"),
            "kuhbs": str(tmp_path / ".kuhbs/my-kuhbs"),
            "desktop_applications": str(tmp_path / ".local/share/applications"),
        },
        "backup": {"kuh": "ndp-kuhbs-usb", "crypt_name": "kuhbs-backups", "ignore_failed_read": True, "dom0_paths": ["/home/user/*"], "max_age_hours": 24},
        "terminal": {"path": "/usr/bin/xfce4-terminal", "args": [], "fallback": {"path": "/usr/bin/xterm", "args": []}},
        "prefs": {"app": {"autostart": False}, "tpl": {"label": "purple"}},
        "services": {"app": {}, "tpl": {}},
        "features": {"app": {}, "tpl": {}},
    }
    logger = EventLogger(log_path=tmp_path / "kuhbs.log", color=False, stdout=False)
    # Create fixtures model both managed VMs as absent before the preflight runs
    fake_runner = MappingRunner(
        logger,
        returncodes={
            ("qvm-check", "--quiet", "app-signal"): 1,
            ("qvm-check", "--quiet", "tpl-signal"): 1,
        },
    )
    return RunnerContext(current_defaults(defaults), logger, fake_runner)


class CreateRestoreTests(unittest.TestCase):
    def test_create_attempts_restore_before_setup_scripts(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            kuhb_dir = tmp_path / ".kuhbs/my-kuhbs/signal"
            (kuhb_dir / "scripts/app").mkdir(parents=True)
            (kuhb_dir / "scripts/app/10-setup.sh").write_text("echo setup\n")
            definition = {
                "id": "signal",
                "name": "Signal",
                "description": "Signal",

                "template": "debian-13-minimal",
                "kuhs": {"tpl": {}, "app": {"instances": [{"id": "default", "prefs": {"label": "green"}}], "backup": {"paths": ["/home/user/.config/Signal"]}}},
            }

            create.run(ctx, merge_kuhb_definition(ctx.defaults, definition))

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn(
                "qvm-run --quiet --user root ndp-kuhbs-usb test -f "
                "/mnt/kuhbs-backup/app-signal.tar.zst",
                log_text,
            )
            self.assertIn("set -o pipefail", log_text)
            self.assertIn("kuhbs.BackupRead + app-signal ndp-kuhbs-usb allow", log_text)
            self.assertIn("qvm-run --quiet --user root app-signal bash -lc", log_text)
            self.assertIn("kuhbs-run-script.sh", log_text)
            self.assertIn("qrexec-client-vm ndp-kuhbs-usb kuhbs.BackupRead | tar -I zstd -xpf - -C /", log_text)
            self.assertLess(log_text.index("Restoring app-signal"), log_text.index("setup-app-signal.sh"))


if __name__ == "__main__":
    unittest.main()
