# Purpose: Regression tests for standalone setup template
# Scope: Uses temp dirs and fake runners instead of mutating real Qubes state
from pathlib import Path
import tempfile
import unittest

from helpers import MappingRunner, RecordingEventLogger as EventLogger, RunnerContext
from kuhbs.model import merge_kuhb_definition
from kuhbs.operations import create
from helpers import current_defaults


def context(tmp_path):
    defaults = {
        "paths": {
            "config": str(tmp_path / ".kuhbs"),
            "scripts": str(tmp_path / ".kuhbs/scripts"),
            "setup_scripts": str(tmp_path / "usr/share/kuhbs/setup-scripts"),
            "user_setup_scripts": str(tmp_path / ".kuhbs/setup-scripts"),
            "kuhbs": str(tmp_path / ".kuhbs/my-kuhbs"),
            "desktop_applications": str(tmp_path / ".local/share/applications"),
        },
        "backup": {"kuh": "ndp-kuhbs-usb", "crypt_name": "kuhbs-backups", "ignore_failed_read": True, "dom0_paths": ["/home/user/*"], "max_age_hours": 24},
        "terminal": {"path": "/usr/bin/xfce4-terminal", "args": [], "fallback": {"path": "/usr/bin/xterm", "args": []}},
        "prefs": {"sta": {"autostart": False}, "tpl": {"label": "purple"}},
        "services": {"sta": {"instances": [{"id": "default"}]}, "tpl": {}},
        "features": {"sta": {"instances": [{"id": "default"}]}, "tpl": {}},
    }
    logger = EventLogger(log_path=tmp_path / "kuhbs.log", color=False, stdout=False)
    # Standalone create fixtures begin without the temporary template or final VMs
    fake_runner = MappingRunner(
        logger,
        returncodes={
            ("qvm-check", "--quiet", "tpl-code-setup-tmp"): 1,
            ("qvm-check", "--quiet", "sta-code-blunix"): 1,
            ("qvm-check", "--quiet", "sta-code-private"): 1,
        },
    )
    return RunnerContext(current_defaults(defaults), logger, fake_runner)


class StandaloneSetupTemplateTests(unittest.TestCase):
    def test_standalone_instances_are_created_from_temporary_setup_template(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            kuhb_dir = tmp_path / ".kuhbs/my-kuhbs/code"
            (kuhb_dir / "hooks/sta-setup-template").mkdir(parents=True)
            (kuhb_dir / "hooks/sta-setup-template/create-pre.sh").write_text("echo setup pre\n")
            (kuhb_dir / "hooks/sta/private").mkdir(parents=True)
            (kuhb_dir / "hooks/sta/private/create-pre.sh").write_text("echo private pre\n")
            (kuhb_dir / "setup/private.sh").parent.mkdir(parents=True)
            (kuhb_dir / "setup/private.sh").write_text("echo private setup\n")
            runner = tmp_path / "usr/share/kuhbs/setup-scripts/kuhbs/kuhbs-run-script.sh"
            runner.parent.mkdir(parents=True)
            runner.write_text("#!/bin/sh\nsh /tmp/kuhbs-script.sh\n", encoding="utf-8")
            definition = {
                "id": "code",
                "name": "Code",
                "description": "Code environments",
                "template": "debian-13-minimal",

                "kuhs": {
                    "sta": {
                        "setup_template": {},
                        "instances": [
                            {"id": "blunix", "prefs": {"label": "green"}},
                            {"id": "private", "prefs": {"label": "yellow"}, "setup_scripts": [str(kuhb_dir / "setup/private.sh")]},
                        ],
                    }
                },
            }

            create.run(ctx, merge_kuhb_definition(ctx.defaults, definition))

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("qvm-clone debian-13-minimal tpl-code-setup-tmp", log_text)
            self.assertIn("qvm-create --class StandaloneVM --label green --template tpl-code-setup-tmp sta-code-blunix", log_text)
            self.assertIn("qvm-create --class StandaloneVM --label yellow --template tpl-code-setup-tmp sta-code-private", log_text)
            self.assertNotIn("qvm-clone tpl-code-setup-tmp sta-code-blunix", log_text)
            self.assertNotIn("qvm-clone tpl-code-setup-tmp sta-code-private", log_text)
            self.assertIn("qvm-remove --force tpl-code-setup-tmp", log_text)
            self.assertLess(log_text.index("qvm-shutdown --wait --force tpl-code-setup-tmp"), log_text.index("qvm-create --class StandaloneVM --label green --template tpl-code-setup-tmp sta-code-blunix"))
            self.assertLess(log_text.index("qvm-create --class StandaloneVM --label yellow --template tpl-code-setup-tmp sta-code-private"), log_text.index("hooks/sta/private/create-pre.sh"))
            self.assertLess(log_text.index("qvm-volume config sta-code-private:private revisions_to_keep 0"), log_text.index("hooks/sta/private/create-pre.sh"))
            self.assertLess(log_text.index("hooks/sta/private/create-pre.sh"), log_text.index("Running setup scripts for sta-code-private"))
            self.assertLess(log_text.index("Running setup scripts for sta-code-private"), log_text.index("qvm-appmenus --update --force sta-code-private"))


if __name__ == "__main__":
    unittest.main()
