# Purpose: Regression tests for operations
# Scope: Uses temp dirs and fake runners instead of mutating real Qubes state
from pathlib import Path
import builtins
from contextlib import redirect_stdout
import io
import tempfile
import unittest
from unittest.mock import Mock, patch

from kuhbs.command import CommandResult, CommandRunner
from helpers import RecordingEventLogger as EventLogger
from kuhbs.model import merge_kuhb_definition
from kuhbs.operations import OperationContext, archive_targets, backup, create, remove, restore, terminal, upgrade_batch
from kuhbs.operations.archive_targets import ArchiveTarget, Dom0ArchiveTarget
from kuhbs.qubes import shutdown_if_running
from kuhbs.state import StateStore
from helpers import MappingRunner, RunnerContext, current_defaults


def context(tmp_path):
    runner = tmp_path / "usr/share/kuhbs/setup-scripts/kuhbs/kuhbs-run-script.sh"
    runner.parent.mkdir(parents=True)
    # Tests install the runner fixture at the configured path instead of relying on repo fallbacks
    runner.write_text("#!/bin/bash\nexit 0\n")
    runner.chmod(0o700)
    apt_update = runner.parent / "setup/apt-update.sh"
    apt_update.parent.mkdir(parents=True)
    # Upgrade tests need the installed shared package-upgrade script without depending on the host install
    apt_update.write_text("apt-get update\n", encoding="utf-8")
    defaults = {
        "paths": {
            "config": str(tmp_path / ".kuhbs"),
            "kuhbs": str(tmp_path / ".kuhbs/my-kuhbs"),
            "desktop_applications": str(tmp_path / ".local/share/applications"),
            "scripts": str(tmp_path / ".kuhbs/scripts"),
            "setup_scripts": str(tmp_path / "usr/share/kuhbs/setup-scripts"),
            "user_setup_scripts": str(tmp_path / ".kuhbs/setup-scripts"),
        },
        "backup": {
            "kuh": "ndp-kuhbs-usb",
            "crypt_name": "kuhbs-backups",
            "ignore_failed_read": True,
            "dom0_paths": ["/home/user/*"],
        },
        "batch_size": {"create": 5, "remove": 5, "backup": 5, "restore": 5, "upgrade": 5},
        "upgrade": {"max_age_minutes": 720, "restart_without_prompt": []},
        "terminal": {"path": "/usr/bin/xfce4-terminal", "args": [], "fallback": {"path": "/usr/bin/xterm", "args": []}},
        "prefs": {"app": {"autostart": False}, "tpl": {"label": "purple"}, "ndp": {"autostart": False}, "sta": {"autostart": False}},
        "services": {"app": {}, "tpl": {}, "ndp": {}, "sta": {"instances": [{"id": "default"}]}},
        "features": {"app": {}, "tpl": {}, "ndp": {}, "sta": {"instances": [{"id": "default"}]}},
    }
    definition = {
        "id": "signal",
        "name": "Signal",
        "description": "Signal is a private messenger kuhb",
        "icon": "icon.svg",
        "order": 500,
        "confirm_network_after_upgrade": False,
        "type": "app",
        "template": "debian-13-minimal",
        "kuhs": {"tpl": {}, "app": {"instances": [{"id": "default", "prefs": {"label": "green"}}]}},
        "setup_scripts": {"tpl": [], "app": []},
    }
    logger = EventLogger(log_path=tmp_path / "kuhbs.log", color=False, stdout=False)
    # Fresh-create fixtures begin without managed VMs; mark_created flips them to existing for other operations
    runner = MappingRunner(
        logger,
        returncodes={
            ("qvm-check", "--quiet", "app-signal"): 1,
            ("qvm-check", "--quiet", "tpl-signal"): 1,
        },
    )
    ctx = RunnerContext(current_defaults(defaults), logger, runner)
    return ctx, merge_kuhb_definition(ctx.defaults, definition)

def mark_created(ctx: OperationContext, kuhb_id: str) -> None:
    # Operation fixtures expose managed VMs only after their lifecycle is marked created
    if isinstance(ctx.runner, MappingRunner):
        for prefix in ("tpl", "app", "ndp", "sta"):
            ctx.runner.returncodes[("qvm-check", "--quiet", f"{prefix}-{kuhb_id}")] = 0
    ctx.set_state(kuhb_id, "create", "completed")


class CompletionInterruptLogger(EventLogger):
    # Simulate Ctrl+C after the completed state file was written but during terminal status output
    def __init__(self, log_path: Path, action: str):
        super().__init__(log_path=log_path, color=False, stdout=False)
        self.action = action

    def status(self, source: str, message: str) -> None:
        super().status(source, message)
        if message == f"State changed: {self.action}:completed":
            raise KeyboardInterrupt


def backup_targets(ctx: OperationContext, definition: dict) -> list[ArchiveTarget]:
    # Backup and restore commands always operate on the parent KUHB selection
    return [ArchiveTarget(kuh.name, definition["id"], definition, kuh) for kuh in backup.backup_enabled_kuhs(ctx, definition)]

def run_backup_targets(ctx: OperationContext, definition: dict, *, source: str = "backup") -> None:
    mark_created(ctx, definition["id"])
    backup.run_archive_targets(ctx, backup_targets(ctx, definition), source=source)

def run_restore_targets(
    ctx: OperationContext,
    definition: dict,
    *,
    source: str = "restore",
    assume_halted: bool = True,
) -> None:
    mark_created(ctx, definition["id"])
    if not assume_halted:
        # Dedicated power-state tests must exercise their runner rather than the generic halted fixture
        restore.run_archive_targets(ctx, backup_targets(ctx, definition), source=source)
        return
    # Generic restore tests isolate extraction behavior while dedicated tests own live power-state cases
    with patch("kuhbs.operations.restore.vm_power_state", return_value="halted"):
        restore.run_archive_targets(ctx, backup_targets(ctx, definition), source=source)


class MissingAppOperationRunner(CommandRunner):
    # Backup/restore must abort when any persistent kuh from the kuhb is missing
    def __init__(self, logger):
        super().__init__(logger)

    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        if log:
            self.logger.command(source, args)
        returncode = 1 if args == ["qvm-check", "--quiet", "app-signal"] else 0
        if log:
            self.logger.exit(source, returncode, " ".join(args))
        if check and returncode != 0:
            raise RuntimeError("unexpected checked missing VM")
        return CommandResult(returncode=returncode)

    def shell(self, command, *, source="dom0", check=True, visible_output=False):
        return self.run(["bash", "-lc", command], source=source, check=check, visible_output=visible_output)


class MissingAppOperationContext(OperationContext):
    @property
    def runner(self):
        return MissingAppOperationRunner(self.logger)


class MissingBackupKuhRunner(CommandRunner):
    # Simulate first-boot create before ndp-kuhbs-usb exists
    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        if log:
            self.logger.command(source, args)
        returncode = 0
        if args == ["qvm-check", "--quiet", "ndp-kuhbs-usb"]:
            returncode = 1
        elif args[:2] == ["qvm-check", "--quiet"] and len(args) == 3:
            returncode = 1
        if log:
            self.logger.exit(source, returncode, " ".join(args))
        if check and returncode != 0:
            raise RuntimeError("unexpected checked missing backup VM")
        return CommandResult(returncode=returncode)

    def shell(self, command, *, source="dom0", check=True, visible_output=False):
        return self.run(["bash", "-lc", command], source=source, check=check, visible_output=visible_output)


class MissingBackupKuhContext(OperationContext):
    @property
    def runner(self):
        return MissingBackupKuhRunner(self.logger)


class RestoreMissingBackupKuhRunner(CommandRunner):
    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        if log:
            self.logger.command(source, args)
        returncode = 1 if args == ["qvm-check", "--quiet", "ndp-kuhbs-usb"] else 0
        if log:
            self.logger.exit(source, returncode, " ".join(args))
        if check and returncode != 0:
            raise RuntimeError("unexpected checked missing backup VM")
        return CommandResult(returncode=returncode)

    def shell(self, command, *, source="dom0", check=True, visible_output=False):
        return self.run(["bash", "-lc", command], source=source, check=check, visible_output=visible_output)


class RestoreMissingBackupKuhContext(OperationContext):
    @property
    def runner(self):
        return RestoreMissingBackupKuhRunner(self.logger)


class MissingArchiveRunner(CommandRunner):
    # Explicit restore must fail when the backup VM exists but the per-kuh archive is absent
    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        if log:
            self.logger.command(source, args)
        returncode = 0
        if args[:7] == [
            "qvm-run",
            "--quiet",
            "--user",
            "root",
            "ndp-kuhbs-usb",
            "test",
            "-f",
        ]:
            returncode = 1
        if log:
            self.logger.exit(source, returncode, " ".join(args))
        if check and returncode != 0:
            raise RuntimeError("unexpected checked missing archive")
        return CommandResult(returncode=returncode)

    def shell(self, command, *, source="dom0", check=True, visible_output=False):
        return self.run(["bash", "-lc", command], source=source, check=check, visible_output=visible_output)


class MissingArchiveContext(OperationContext):
    @property
    def runner(self):
        return MissingArchiveRunner(self.logger)


class MissingBackupPathRunner(CommandRunner):
    # Batch backup/restore preflights must be target-level failures so state markers update
    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        if log:
            self.logger.command(source, args)
        returncode = 1 if args == [
            "qvm-run",
            "--quiet",
            "--user",
            "root",
            "ndp-kuhbs-usb",
            "/usr/bin/test",
            "-d",
            "/mnt/kuhbs-backup",
        ] else 0
        if log:
            self.logger.exit(source, returncode, " ".join(args))
        if check and returncode != 0:
            raise RuntimeError("backup path missing")
        return CommandResult(returncode=returncode)

    def shell(self, command, *, source="dom0", check=True, visible_output=False):
        return self.run(["bash", "-lc", command], source=source, check=check, visible_output=visible_output)


class MissingBackupPathContext(OperationContext):
    @property
    def runner(self):
        return MissingBackupPathRunner(self.logger)


class FailingBackupMoveRunner(CommandRunner):
    def __init__(self, logger):
        super().__init__(logger)
        self.commands: list[list[str]] = []

    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        self.commands.append(list(args))
        if log:
            self.logger.command(source, args)
        returncode = 1 if args[:5] == ["qvm-run", "--quiet", "--user", "root", "app-signal"] and "qrexec-client-vm ndp-kuhbs-usb kuhbs.BackupWrite" in " ".join(args) else 0
        if log:
            self.logger.exit(source, returncode, " ".join(args))
        if check and returncode != 0:
            raise RuntimeError("backup stream failed")
        return CommandResult(returncode=returncode)

    def shell(self, command, *, source="dom0", check=True, visible_output=False):
        return self.run(["bash", "-lc", command], source=source, check=check, visible_output=visible_output)


class FailingRestoreExtractRunner(CommandRunner):
    def __init__(self, logger):
        super().__init__(logger)
        self.commands: list[list[str]] = []

    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        self.commands.append(list(args))
        if log:
            self.logger.command(source, args)
        returncode = 0
        if args == ["qvm-check", "--quiet", "--running", "app-signal"]:
            returncode = 1
        elif args[:5] == ["qvm-run", "--quiet", "--user", "root", "app-signal"] and "kuhbs.BackupRead" in " ".join(args):
            returncode = 1
        if log:
            self.logger.exit(source, returncode, " ".join(args))
        if check and returncode != 0:
            raise RuntimeError("extract failed")
        return CommandResult(returncode=returncode)

    def shell(self, command, *, source="dom0", check=True, visible_output=False):
        return self.run(["bash", "-lc", command], source=source, check=check, visible_output=visible_output)


class RestoreRunningStateRunner(CommandRunner):
    # Restore must remember whether each target kuh was running before qvm-run starts it
    def __init__(self, logger, *, running_before=False):
        super().__init__(logger)
        self.running_before = running_before
        self.commands: list[list[str]] = []

    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        self.commands.append(list(args))
        if log:
            self.logger.command(source, args)
        returncode = 0
        if args[:2] == ["qvm-check", "--quiet"]:
            if len(args) >= 4 and args[2] == "--paused":
                returncode = 1
            elif len(args) >= 4 and args[2] == "--running":
                # Backup infrastructure is already running; running_before models only app-signal
                returncode = 0 if args[3] == "ndp-kuhbs-usb" or self.running_before else 1
            else:
                returncode = 0
        elif args[:5] == ["qvm-run", "--quiet", "--user", "root", "app-signal"]:
            # Restore extraction starts a previously halted target before cleanup checks its live state
            self.running_before = True
        if log:
            self.logger.exit(source, returncode, " ".join(args))
        if check and returncode != 0:
            raise RuntimeError("unexpected checked command")
        return CommandResult(returncode=returncode)

    def shell(self, command, *, source="dom0", check=True, visible_output=False):
        return self.run(["bash", "-lc", command], source=source, check=check, visible_output=visible_output)


class RestoreRunningStateContext(OperationContext):
    def __init__(self, defaults, logger, runner):
        super().__init__(defaults, logger)
        self._runner = runner

    @property
    def runner(self):
        return self._runner


class UpgradeRunner(CommandRunner):
    # Upgrade tests need real command return codes without mutating Qubes or opening terminals
    def __init__(self, logger, *, existing=None, running=None, prefs=None):
        super().__init__(logger)
        self.existing = set(existing or [])
        self.running = set(running or [])
        self.prefs = prefs or {}
        self.commands: list[list[str]] = []

    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        self.commands.append(list(args))
        if log:
            self.logger.command(source, args)
        returncode = 0
        stdout = ""
        if args[:2] == ["qvm-check", "--quiet"]:
            # qvm-check is the upgrade preflight for existence and prior running state
            if len(args) >= 4 and args[2] == "--running":
                returncode = 0 if args[3] in self.running else 1
            else:
                returncode = 0 if args[2] in self.existing else 1
        elif args == ["qvm-ls", "--raw-list", "--running"]:
            stdout = "\n".join(sorted(self.running)) + ("\n" if self.running else "")
        elif args == ["xl", "list"]:
            # QubesStatus uses xl state instead of qvm-ls so dependent discovery has one live-state probe
            rows = ["Name ID Mem VCPUs State Time(s)"]
            rows.extend(f"{name} 1 1 1 -b---- 1.0" for name in sorted(self.running))
            stdout = "\n".join(rows) + "\n"
        elif len(args) == 3 and args[0] == "qvm-prefs":
            value = self.prefs.get((args[1], args[2]))
            if value is None:
                returncode = 1
            else:
                stdout = f"{value}\n"
        if log:
            self.logger.exit(source, returncode, " ".join(args))
        if check and returncode != 0:
            raise RuntimeError("unexpected checked command")
        return CommandResult(returncode=returncode, stdout=stdout)

    def shell(self, command, *, source="dom0", check=True, visible_output=False):
        return self.run(["bash", "-lc", command], source=source, check=check, visible_output=visible_output)

    def start(self, args, *, source="dom0"):
        # VM terminals are simulated; hook tests care about orchestration commands, not an actual qvm-run process
        self.commands.append(list(args))
        self.logger.command(source, args)
        return None

    def poll(self, process):
        return 0

    def finish(self, process, args, *, source="dom0", check=True, log=True):
        if log:
            self.logger.exit(source, 0, " ".join(args))
        return CommandResult(returncode=0)


def write_upgrade_qubes_xml(defaults: dict, runner: UpgradeRunner) -> None:
    # Upgrade dependent discovery reads qubes.xml once instead of qvm-prefs per running VM
    path = Path(defaults["paths"]["config"]).parent / "qubes.xml"
    defaults["paths"]["qubes_xml"] = str(path)
    names = set(runner.existing) | set(runner.running) | {name for name, _pref in runner.prefs}
    rows = []
    for name in sorted(names):
        klass = runner.prefs.get((name, "klass"))
        if klass is None:
            klass = "TemplateVM" if name.startswith("tpl-") else "DispVM" if name.startswith("disp") else "AppVM"
        props = [f'<property name="name">{name}</property>', '<property name="label">green</property>']
        template = runner.prefs.get((name, "template"))
        if template:
            props.append(f'<property name="template">{template}</property>')
        dispvm_template = runner.prefs.get((name, "dispvm_template"))
        if dispvm_template:
            props.append(f'<property name="dispvm_template">{dispvm_template}</property>')
        rows.append(f'<domain class="{klass}"><properties>{"".join(props)}</properties></domain>')
    path.write_text('<qubes><domains>' + "".join(rows) + '</domains></qubes>', encoding="utf-8")


class UpgradeContext(OperationContext):
    # Return one runner instance so tests can assert the complete upgrade command flow
    def __init__(self, defaults, logger, runner):
        write_upgrade_qubes_xml(defaults, runner)
        super().__init__(defaults, logger)
        self._runner = runner

    @property
    def runner(self):
        return self._runner


class InputPatch:
    # Keep confirmation tests explicit and quiet without pulling in mock for one tiny monkeypatch
    def __init__(self, answer: str):
        self.answer = answer
        self.original = builtins.input
        self.stdout = io.StringIO()
        self.redirect = redirect_stdout(self.stdout)

    def __enter__(self):
        self.redirect.__enter__()

        def fake_input(prompt=""):
            # Tests capture the prompt text because input() writes it outside normal print output
            print(prompt, end="")
            return self.answer

        builtins.input = fake_input
        return self

    def __exit__(self, exc_type, exc, tb):
        builtins.input = self.original
        self.redirect.__exit__(exc_type, exc, tb)


class CreateAllOperationTests(unittest.TestCase):
    def test_run_all_stops_after_failed_order_group(self):
        ctx = type("Ctx", (), {"logger": Mock(), "defaults": {"batch_size": {"create": 5}}})()
        definitions = (
            {"id": "later", "order": 20},
            {"id": "alpha", "order": 10},
            {"id": "bravo", "order": 10},
        )
        calls: list[str] = []

        def fake_run(_ctx, definition):
            calls.append(definition["id"])
            if definition["id"] == "bravo":
                raise RuntimeError("boom")

        with patch("kuhbs.operations.create.run", side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, "create failed at order 10"):
                create.run_all(ctx, definitions)

        self.assertEqual(set(calls), {"alpha", "bravo"})
        self.assertNotIn("later", calls)


class OperationTests(unittest.TestCase):
    def test_create_completion_interrupt_keeps_completed_state(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            logger = CompletionInterruptLogger(tmp_path / "events.log", "create")
            # Preserve the fresh-VM map while swapping in the logger that interrupts final output
            ctx = RunnerContext(
                base_ctx.defaults,
                logger,
                MappingRunner(logger, returncodes=dict(base_ctx.runner.returncodes)),
            )

            with self.assertRaises(KeyboardInterrupt):
                create.run(ctx, definition)

            self.assertEqual(ctx.state_store.get_state("signal", "create"), "completed")

    def test_remove_completion_interrupt_keeps_completed_state(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            logger = CompletionInterruptLogger(tmp_path / "events.log", "remove")
            ctx = RunnerContext(base_ctx.defaults, logger, MappingRunner(logger))
            mark_created(ctx, "signal")

            with self.assertRaises(KeyboardInterrupt):
                remove.run(ctx, definition)

            self.assertEqual(ctx.state_store.get_state("signal", "remove"), "completed")

    def test_archive_completion_interrupt_keeps_completed_state(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            logger = CompletionInterruptLogger(tmp_path / "events.log", "backup")
            ctx = RunnerContext(base_ctx.defaults, logger, MappingRunner(logger))
            target = ArchiveTarget("app-signal", "signal", definition, None)
            job = archive_targets.ArchiveJob("signal", (target,))

            with self.assertRaises(KeyboardInterrupt):
                archive_targets._run_state_job(ctx, job, lambda _target: None, action="backup")

            self.assertEqual(ctx.state_store.get_state("signal", "backup"), "completed")

    def test_dom0_archive_completion_interrupt_keeps_completed_state(self):
        for action, operation, preflight in (
            ("backup", backup, None),
            ("restore", restore, "_preflight_restore_targets"),
        ):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as td:
                tmp_path = Path(td)
                base_ctx, _definition = context(tmp_path)
                logger = CompletionInterruptLogger(tmp_path / "events.log", action)
                ctx = RunnerContext(base_ctx.defaults, logger, MappingRunner(logger))
                patches = [patch.object(operation, "run_dom0")]
                if preflight is not None:
                    patches.append(patch.object(operation, preflight))

                with patches[0]:
                    if len(patches) == 1:
                        with self.assertRaises(KeyboardInterrupt):
                            operation.run_archive_targets(ctx, [Dom0ArchiveTarget()])
                    else:
                        with patches[1], self.assertRaises(KeyboardInterrupt):
                            operation.run_archive_targets(ctx, [Dom0ArchiveTarget()])

                self.assertEqual(ctx.state_store.get_state("dom0", action), "completed")

    def test_shutdown_if_running_skips_disappeared_vm(self):
        with tempfile.TemporaryDirectory() as td:
            logger = EventLogger(Path(td) / "events.log", color=False, stdout=False)
            check = ("qvm-check", "--quiet", "--running", "disp1234")
            runner = MappingRunner(logger, returncodes={check: 1})

            shutdown_if_running(runner, "disp1234")

            self.assertEqual(runner.commands, [list(check)])

    def test_create_emits_expected_qvm_flow_and_sets_created_state(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx, definition = context(tmp_path)
            kuhb_dir = tmp_path / ".kuhbs/my-kuhbs/signal"
            (kuhb_dir / "hooks/tpl").mkdir(parents=True)
            (kuhb_dir / "hooks/app").mkdir(parents=True)
            (kuhb_dir / "scripts/tpl").mkdir(parents=True)
            (kuhb_dir / "scripts/app").mkdir(parents=True)
            (kuhb_dir / "hooks/tpl/create-pre.sh").write_text("echo tpl pre\n")
            (kuhb_dir / "hooks/app/create-pre.sh").write_text("echo app pre\n")
            (kuhb_dir / "scripts/tpl/10-setup.sh").write_text("echo tpl setup\n")
            (kuhb_dir / "scripts/app/10-setup.sh").write_text("echo app setup\n")

            create.run(ctx, definition)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("qvm-clone debian-13-minimal tpl-signal", log_text)
            self.assertIn("COMMAND   tpl-signal", log_text)
            self.assertIn("qvm-create --class AppVM --label green --template tpl-signal app-signal", log_text)
            self.assertIn("COMMAND   app-signal", log_text)
            self.assertLess(log_text.index("qvm-clone debian-13-minimal tpl-signal"), log_text.index("hooks/tpl/create-pre.sh"))
            self.assertLess(log_text.index("hooks/tpl/create-pre.sh"), log_text.index("Running setup scripts for tpl-signal"))
            self.assertLess(log_text.index("qvm-create --class AppVM --label green --template tpl-signal app-signal"), log_text.index("hooks/app/create-pre.sh"))
            self.assertLess(log_text.index("hooks/app/create-pre.sh"), log_text.index("Running setup scripts for app-signal"))
            self.assertEqual(StateStore(tmp_path / ".kuhbs/states").get_state("signal", "create"), "completed")

    def test_create_serializes_trusted_qvm_scalars_without_feature_semantics(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx, definition = context(tmp_path)
            app = definition["kuhs"]["app"]["instances"][0]
            app["prefs"]["custom-ratio"] = 1.5
            app["features"] = {"number": 2.5, "boolean": True, "null": None}

            create.run(ctx, definition)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("qvm-prefs app-signal custom-ratio 1.5", log_text)
            self.assertIn("qvm-features app-signal number 2.5", log_text)
            self.assertIn("qvm-features app-signal boolean True", log_text)
            self.assertIn("qvm-features app-signal null None", log_text)

    def test_kuhb_log_source_uses_kuhbs_purple(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx, definition = context(tmp_path)

            ctx.register_kuh_labels(definition)

            # The kuhb id is a brand/source label, while VM source colors keep Qubes labels
            self.assertEqual("purple", ctx.logger._source_color("signal"))
            self.assertEqual("green", ctx.logger._source_color("app-signal"))

    def test_create_preflights_every_target_before_existing_app_collision(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            runner = MappingRunner(
                base_ctx.logger,
                returncodes={
                    ("qvm-check", "--quiet", "app-signal"): 0,
                    ("qvm-check", "--quiet", "tpl-signal"): 1,
                },
            )
            ctx = RunnerContext(base_ctx.defaults, base_ctx.logger, runner)

            with self.assertRaisesRegex(RuntimeError, "refusing to create signal; VM already exists: app-signal"):
                create.run(ctx, definition)

            # All collisions are known before any create command can leave a mixed old/new KUHB
            self.assertIn(["qvm-check", "--quiet", "app-signal"], runner.commands)
            self.assertIn(["qvm-check", "--quiet", "tpl-signal"], runner.commands)
            self.assertFalse(any(command[0] in {"qvm-create", "qvm-clone"} for command in runner.commands))
            self.assertEqual(StateStore(tmp_path / ".kuhbs/states").get_state("signal", "create"), "failed")

    def test_remove_deletes_resources_and_upgrade_history_but_not_definition(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx, definition = context(tmp_path)
            kuhb_dir = tmp_path / ".kuhbs/my-kuhbs/signal"
            upgrade_dir = tmp_path / ".kuhbs/upgrades"
            kuhb_dir.mkdir(parents=True)
            upgrade_dir.mkdir(parents=True)
            (upgrade_dir / "tpl-signal").write_text("2026-01-01T00:00:00+00:00\n")
            (upgrade_dir / "app-signal").write_text("2026-01-01T00:00:00+00:00\n")
            mark_created(ctx, "signal")

            remove.run(ctx, definition)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("qvm-kill app-signal", log_text)
            self.assertIn("qvm-remove --force app-signal", log_text)
            self.assertIn("qvm-kill tpl-signal", log_text)
            self.assertIn("qvm-remove --force tpl-signal", log_text)
            self.assertTrue(kuhb_dir.exists())
            # Recreated VMs must not inherit freshness markers from resources that Remove deleted
            self.assertFalse((upgrade_dir / "tpl-signal").exists())
            self.assertFalse((upgrade_dir / "app-signal").exists())

    def test_remove_continues_when_one_vm_is_already_absent(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            ctx = RunnerContext(base_ctx.defaults, base_ctx.logger, MappingRunner(base_ctx.logger))
            mark_created(ctx, "signal")
            # This case overrides the normal post-create fixture to model one externally missing VM
            ctx.runner.returncodes[("qvm-check", "--quiet", "app-signal")] = 1

            remove.run(ctx, definition)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertLess(log_text.index("qvm-check --quiet app-signal"), log_text.index("qvm-remove --force tpl-signal"))
            self.assertIn("EXIT      app-signal                1", log_text)
            self.assertNotIn("qvm-kill app-signal", log_text)
            self.assertNotIn("qvm-remove --force app-signal", log_text)
            self.assertIn("qvm-kill tpl-signal", log_text)
            self.assertIn("EXIT      tpl-signal                0", log_text)
            self.assertEqual(StateStore(tmp_path / ".kuhbs/states").get_state("signal", "remove"), "completed")

    def test_backup_checks_destination_directory_only(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx, definition = context(tmp_path)
            definition["kuhs"] = {"app": {"instances": [{"id": "default", "backup": {"paths": ["/home/user/.config/Signal"]}}]}}

            run_backup_targets(ctx, definition)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("test -d /mnt/kuhbs-backup", log_text)

    def test_backup_archives_shell_expanded_configured_kuh_paths(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx, definition = context(tmp_path)
            definition["kuhs"] = {"tpl": {}, "app": {"instances": [{"id": "default", "backup": {"paths": ["/home/user/*"]}}]}}

            run_backup_targets(ctx, definition)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("set -o pipefail", log_text)
            self.assertIn("qvm-run --quiet --user root app-signal bash -lc", log_text)
            self.assertIn("zstd -T0 -3", log_text)
            self.assertIn("-cpf - --ignore-failed-read -- /home/user/*", log_text)
            self.assertIn("qrexec-client-vm ndp-kuhbs-usb kuhbs.BackupWrite", log_text)
            self.assertIn("set -o pipefail; tar -I", log_text)
            self.assertIn("kuhbs.BackupWrite + app-signal ndp-kuhbs-usb allow", log_text)

    def test_backup_silently_skips_app_without_backup_paths(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx, definition = context(tmp_path)

            run_backup_targets(ctx, definition)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertNotIn("tar -I 'zstd -T0 -3' -cpf", log_text)

    def test_backup_aborts_when_persistent_kuh_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            definition["kuhs"] = {"tpl": {}, "app": {"instances": [{"id": "default", "backup": {"paths": ["/home/user/.config/Signal"]}}]}}
            ctx = MissingAppOperationContext(base_ctx.defaults, base_ctx.logger)

            # A kuhb backup must be complete; silently skipping a missing VM would create a misleading backup
            with self.assertRaisesRegex(RuntimeError, "Required kuh missing: app-signal"):
                run_backup_targets(ctx, definition)
            self.assertIn("Required kuh missing: app-signal", (tmp_path / "kuhbs.log").read_text())

    def test_backup_silently_skips_standalone_without_backup_paths(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx, definition = context(tmp_path)
            definition["kuhs"] = {"sta": {"instances": [{"id": "default"}]}}

            run_backup_targets(ctx, definition)

    def test_backup_removes_qrexec_policy_after_stream_failure(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            definition["kuhs"] = {"app": {"instances": [{"id": "default", "backup": {"paths": ["/home/user/.config/Signal"]}}]}}
            runner = FailingBackupMoveRunner(base_ctx.logger)
            ctx = RunnerContext(base_ctx.defaults, base_ctx.logger, runner)
            kuh = backup.backup_enabled_kuhs(ctx, definition)[0]

            with self.assertRaisesRegex(RuntimeError, "backup stream failed"):
                backup.run_kuh_archive(ctx, definition, kuh, was_running=False)

            self.assertIn(["sudo", "rm", "-f", "/etc/qubes/policy.d/30-kuhbs-backup-write-app-signal.policy"], runner.commands)
            self.assertIn(["qvm-shutdown", "--wait", "--force", "app-signal"], runner.commands)

    def test_backup_unpauses_paused_target_inside_its_worker(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx, definition = context(tmp_path)
            definition["kuhs"] = {
                "app": {
                    "instances": [
                        {
                            "id": "default",
                            "backup": {"paths": ["/home/user/.config/Signal"]},
                        },
                    ],
                },
            }
            kuh = backup.backup_enabled_kuhs(ctx, definition)[0]

            # A paused source becomes usable only after its bounded backup worker starts
            with patch.object(backup, "restore_vm_power_state") as restore_state:
                backup.run_kuh_archive(ctx, definition, kuh, original_state="paused")

            self.assertIn(["qvm-unpause", "app-signal"], ctx.runner.commands)
            restore_state.assert_called_once_with(ctx.runner, "app-signal", "paused")

    def test_backup_all_preflight_keeps_state_when_storage_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            definition["kuhs"] = {"app": {"instances": [{"id": "default", "backup": {"paths": ["/home/user/.config/Signal"]}}]}}
            base_ctx.defaults["batch_size"]["backup"] = 2
            ctx = MissingBackupPathContext(base_ctx.defaults, base_ctx.logger)
            mark_created(ctx, "signal")
            target = ArchiveTarget("app-signal", "signal", definition, backup.backup_enabled_kuhs(ctx, definition)[0])

            with self.assertRaisesRegex(RuntimeError, "Backup storage is not mounted"):
                backup.run_archive_targets(ctx, [target], source="backup-all")

            self.assertIsNone(ctx.state_store.get_state("signal", "backup"))

    def test_backup_supports_shell_escaped_spaces_in_source_paths(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx, definition = context(tmp_path)
            source = r"/home/user/My\ Favorite\ Files"
            definition["kuhs"] = {
                "app": {
                    "instances": [
                        {"id": "default", "backup": {"paths": [source]}},
                    ],
                },
            }

            run_backup_targets(ctx, definition)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn(source, log_text)

    def test_backup_kuh_inherits_strict_source_reads(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx, definition = context(tmp_path)
            ctx.defaults["backup"]["ignore_failed_read"] = False
            definition["kuhs"] = {
                "app": {
                    "instances": [
                        {
                            "id": "default",
                            "backup": {"paths": ["/home/user/.config/Signal"]},
                        },
                    ],
                },
            }

            run_backup_targets(ctx, definition)

            # Missing sources remain fatal unless this concrete VM opts out
            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertNotIn("--ignore-failed-read", log_text)

    def test_backup_kuh_can_ignore_live_source_read_failures(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx, definition = context(tmp_path)
            ctx.defaults["backup"]["ignore_failed_read"] = False
            definition["kuhs"] = {
                "app": {
                    "instances": [
                        {
                            "id": "default",
                            "backup": {
                                "paths": ["/home/user/.thunderbird"],
                                "ignore_failed_read": True,
                            },
                        },
                    ],
                },
            }

            run_backup_targets(ctx, definition)

            # Busy applications may accept tar read warnings for their own archive
            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("--ignore-failed-read", log_text)

    def test_restore_checks_destination_directory_only(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx, definition = context(tmp_path)
            definition["kuhs"] = {"app": {"instances": [{"id": "default", "backup": {"paths": ["/home/user/.config/Signal"]}}]}}

            run_restore_targets(ctx, definition)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("test -d /mnt/kuhbs-backup", log_text)

    def test_create_warns_and_skips_restore_when_backup_kuh_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            definition["kuhs"]["app"]["instances"][0]["backup"] = {"paths": ["/home/user/.config/Signal"]}
            ctx = MissingBackupKuhContext(base_ctx.defaults, base_ctx.logger)

            create.run(ctx, definition)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("WARNING   signal", log_text)
            self.assertIn("BACKUP VM ndp-kuhbs-usb DOES NOT EXIST! SKIPPING RESTORE!", log_text)
            self.assertNotIn("qvm-run --quiet --user root ndp-kuhbs-usb test -f", log_text)

    def test_restore_errors_when_backup_kuh_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            definition["kuhs"] = {"app": {"instances": [{"id": "default", "backup": {"paths": ["/home/user/.config/Signal"]}}]}}
            ctx = RestoreMissingBackupKuhContext(base_ctx.defaults, base_ctx.logger)

            with self.assertRaisesRegex(
                RuntimeError,
                "BACKUP VM ndp-kuhbs-usb DOES NOT EXIST",
            ):
                run_restore_targets(ctx, definition)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("ERROR     restore", log_text)
            self.assertIn("BACKUP VM ndp-kuhbs-usb DOES NOT EXIST! ABORTING RESTORE!", log_text)
            self.assertNotIn("qvm-run --quiet --user root ndp-kuhbs-usb test -d", log_text)
            self.assertIsNone(StateStore(tmp_path / ".kuhbs/states").get_state("signal", "restore"))

    def test_restore_errors_when_archive_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            definition["kuhs"] = {"app": {"instances": [{"id": "default", "backup": {"paths": ["/home/user/.config/Signal"]}}]}}
            ctx = MissingArchiveContext(base_ctx.defaults, base_ctx.logger)

            with self.assertRaisesRegex(RuntimeError, "No backup found at app-signal"):
                run_restore_targets(ctx, definition)
            self.assertIn("No backup found at app-signal:/mnt/kuhbs-backup/app-signal.tar.zst", (tmp_path / "kuhbs.log").read_text())

            self.assertIsNone(StateStore(tmp_path / ".kuhbs/states").get_state("signal", "restore"))

    def test_create_warns_when_archive_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            definition["kuhs"]["app"]["instances"][0]["backup"] = {"paths": ["/home/user/.config/Signal"]}
            ctx = MissingArchiveContext(base_ctx.defaults, base_ctx.logger)

            # Missing-archive coverage starts from fresh create targets while its runner models available backup storage
            with patch("kuhbs.operations.create.vm_exists", return_value=False):
                create.run(ctx, definition)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("WARNING   signal", log_text)
            self.assertIn("No backup found at app-signal:/mnt/kuhbs-backup/app-signal.tar.zst", log_text)
            self.assertEqual(StateStore(tmp_path / ".kuhbs/states").get_state("signal", "create"), "completed")

    def test_restore_dom0_errors_when_backup_kuh_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, _definition = context(tmp_path)
            ctx = MissingBackupKuhContext(base_ctx.defaults, base_ctx.logger)

            with self.assertRaisesRegex(RuntimeError, "BACKUP VM ndp-kuhbs-usb DOES NOT EXIST! ABORTING RESTORE!"):
                restore.run_archive_targets(ctx, [Dom0ArchiveTarget()])

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("ERROR     restore", log_text)
            self.assertIn("BACKUP VM ndp-kuhbs-usb DOES NOT EXIST! ABORTING RESTORE!", log_text)
            self.assertNotIn("qvm-run --quiet --user root ndp-kuhbs-usb test -d", log_text)

    def test_restore_preflights_dom0_archive_before_extracting_vm(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            definition["kuhs"] = {
                "app": {
                    "instances": [
                        {
                            "id": "default",
                            "backup": {"paths": ["/home/user/.config/Signal"]},
                        },
                    ],
                },
            }
            dom0_archive = [
                "qvm-run",
                "--quiet",
                "--user",
                "root",
                "ndp-kuhbs-usb",
                "test",
                "-f",
                "/mnt/kuhbs-backup/dom0.tar.zst",
            ]
            runner = MappingRunner(
                base_ctx.logger,
                returncodes={
                    tuple(["qvm-check", "--quiet", "--running", "app-signal"]): 1,
                    tuple(dom0_archive): 1,
                },
            )
            ctx = RunnerContext(base_ctx.defaults, base_ctx.logger, runner)
            mark_created(ctx, "signal")
            targets = [
                *backup_targets(ctx, definition),
                Dom0ArchiveTarget(),
            ]

            with self.assertRaisesRegex(RuntimeError, "No dom0 backup found"):
                restore.run_archive_targets(ctx, targets)

            self.assertFalse(
                any("kuhbs.BackupRead" in " ".join(command) for command in runner.commands)
            )

    def test_restore_uses_qrexec_backup_read_stream(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx, definition = context(tmp_path)
            definition["kuhs"] = {"app": {"instances": [{"id": "default", "backup": {"paths": ["/home/user/.config/Signal"]}}]}}

            run_restore_targets(ctx, definition)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("set -o pipefail", log_text)
            self.assertIn("kuhbs.BackupRead + app-signal ndp-kuhbs-usb allow", log_text)
            self.assertIn("qvm-run --quiet --user root app-signal bash -lc", log_text)
            self.assertIn("qrexec-client-vm ndp-kuhbs-usb kuhbs.BackupRead | tar -I zstd -xpf - -C /", log_text)

    def test_restore_preserves_halted_target_state(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            definition["kuhs"] = {"app": {"instances": [{"id": "default", "backup": {"paths": ["/home/user/.config/Signal"]}}]}}
            runner = RestoreRunningStateRunner(base_ctx.logger, running_before=False)
            ctx = RestoreRunningStateContext(base_ctx.defaults, base_ctx.logger, runner)

            run_restore_targets(ctx, definition)

            self.assertIn(["qvm-check", "--quiet", "--running", "app-signal"], runner.commands)
            self.assertIn(["qvm-shutdown", "--wait", "--force", "app-signal"], runner.commands)

    def test_restore_refuses_running_target_before_extract(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            definition["kuhs"] = {"app": {"instances": [{"id": "default", "backup": {"paths": ["/home/user/.config/Signal"]}}]}}
            runner = RestoreRunningStateRunner(base_ctx.logger, running_before=True)
            ctx = RestoreRunningStateContext(base_ctx.defaults, base_ctx.logger, runner)

            with self.assertRaisesRegex(RuntimeError, "Restore requires halted kuh: app-signal"):
                run_restore_targets(ctx, definition, assume_halted=False)

            self.assertIn(["qvm-check", "--quiet", "--running", "app-signal"], runner.commands)
            self.assertNotIn(["qvm-shutdown", "--wait", "--force", "app-signal"], runner.commands)
            self.assertFalse(any("kuhbs.BackupRead" in " ".join(command) for command in runner.commands))

    def test_restore_preflights_every_archive_before_extracting_any(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            definition["kuhs"] = {
                "app": {
                    "instances": [
                        {
                            "id": "default",
                            "backup": {"paths": ["/home/user/first"]},
                        },
                        {
                            "id": "second",
                            "backup": {"paths": ["/home/user/second"]},
                        },
                    ],
                },
            }
            missing = [
                "qvm-run",
                "--quiet",
                "--user",
                "root",
                "ndp-kuhbs-usb",
                "test",
                "-f",
                "/mnt/kuhbs-backup/app-signal-second.tar.zst",
            ]
            halted = {
                tuple(["qvm-check", "--quiet", "--running", name]): 1
                for name in ("app-signal", "app-signal-second")
            }
            runner = MappingRunner(
                base_ctx.logger,
                returncodes={**halted, tuple(missing): 1},
            )
            ctx = RunnerContext(base_ctx.defaults, base_ctx.logger, runner)

            with self.assertRaisesRegex(
                RuntimeError,
                "No backup found at app-signal-second",
            ):
                run_restore_targets(ctx, definition)

            self.assertFalse(
                any("kuhbs.BackupRead" in " ".join(command) for command in runner.commands)
            )

    def test_restore_workers_trust_the_completed_full_request_preflight(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            definition["kuhs"] = {"app": {"instances": [{"id": "default", "backup": {"paths": ["/home/user/.config/Signal"]}}]}}
            runner = MappingRunner(
                base_ctx.logger,
                returncodes={("qvm-check", "--quiet", "--running", "app-signal"): 1},
            )
            ctx = RunnerContext(base_ctx.defaults, base_ctx.logger, runner)

            run_restore_targets(ctx, definition, assume_halted=False)

            # Execution uses one preflight plus one post-extraction running check before cleanup shutdown
            self.assertEqual(runner.commands.count(["qvm-check", "--quiet", "ndp-kuhbs-usb"]), 1)
            self.assertEqual(runner.commands.count(["qvm-check", "--quiet", "--running", "app-signal"]), 2)

    def test_restore_uses_qrexec_backup_read_for_every_restore(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx, definition = context(tmp_path)
            definition["kuhs"] = {"app": {"instances": [{"id": "default", "backup": {"paths": ["/home/user/.config/Signal"]}}]}}
            run_restore_targets(ctx, definition)

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("kuhbs.BackupRead + app-signal ndp-kuhbs-usb allow", log_text)
            self.assertIn("qrexec-client-vm ndp-kuhbs-usb kuhbs.BackupRead | tar -I zstd -xpf - -C /", log_text)

    def test_restore_removes_qrexec_policy_after_extract_failure(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            definition["kuhs"] = {"app": {"instances": [{"id": "default", "backup": {"paths": ["/home/user/.config/Signal"]}}]}}
            runner = FailingRestoreExtractRunner(base_ctx.logger)
            ctx = RunnerContext(base_ctx.defaults, base_ctx.logger, runner)
            kuh = backup.backup_enabled_kuhs(ctx, definition)[0]

            with self.assertRaisesRegex(RuntimeError, "extract failed"):
                restore.restore_kuh_archive(ctx, definition, kuh, was_running=False)

            self.assertIn(["sudo", "rm", "-f", "/etc/qubes/policy.d/30-kuhbs-backup-read-app-signal.policy"], runner.commands)

    def test_restore_all_preflight_keeps_state_when_storage_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            definition["kuhs"] = {"app": {"instances": [{"id": "default", "backup": {"paths": ["/home/user/.config/Signal"]}}]}}
            base_ctx.defaults["batch_size"]["backup"] = 2
            ctx = MissingBackupPathContext(base_ctx.defaults, base_ctx.logger)
            mark_created(ctx, "signal")
            target = ArchiveTarget("app-signal", "signal", definition, backup.backup_enabled_kuhs(ctx, definition)[0])

            with self.assertRaisesRegex(RuntimeError, "Backup storage is not mounted"):
                restore.run_archive_targets(ctx, [target], source="restore-all")

            self.assertIsNone(ctx.state_store.get_state("signal", "restore"))

    def test_restore_aborts_when_persistent_kuh_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            definition["kuhs"] = {"app": {"instances": [{"id": "default", "backup": {"paths": ["/home/user/.config/Signal"]}}]}}
            ctx = MissingAppOperationContext(base_ctx.defaults, base_ctx.logger)

            # Restore should not make a broken kuhb look OK by restoring only the VMs that still exist
            with self.assertRaisesRegex(RuntimeError, "Required kuh missing: app-signal"):
                run_restore_targets(ctx, definition)
            self.assertIn("Required kuh missing: app-signal", (tmp_path / "kuhbs.log").read_text())

    def test_upgrade_runs_update_hooks_for_created_kuhs(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx, definition = context(tmp_path)

            # Hook coverage declines the unrelated restart prompt owned by dedicated upgrade tests
            with patch("builtins.input", return_value="n"):
                upgrade_batch.run_targets(ctx, [definition])

            self.assertIn("STATUS    signal", (tmp_path / "kuhbs.log").read_text())

    def test_upgrade_keeps_apt_out_of_appvm_without_update_hook(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            runner = UpgradeRunner(base_ctx.logger, existing={"tpl-signal", "app-signal"})
            ctx = UpgradeContext(base_ctx.defaults, base_ctx.logger, runner)

            upgrade_batch.run_targets(ctx, [definition])

            # AppVM roots are template-backed, so the shared apt script belongs in the TemplateVM only
            copied_to_templates = [command for command in runner.commands if command[:2] == ["qvm-copy-to-vm", "tpl-signal"]]
            copied_to_apps = [command for command in runner.commands if command[:2] == ["qvm-copy-to-vm", "app-signal"]]
            self.assertTrue(any(any(path.endswith("/setup/apt-update.sh") for path in command) for command in copied_to_templates))
            self.assertFalse(any(any(path.endswith("/setup/apt-update.sh") for path in command) for command in copied_to_apps))
            self.assertIn("Skipping app-signal; no update hook", (tmp_path / "kuhbs.log").read_text())

    def test_upgrade_runs_shared_apt_script_in_terminal_before_update_hook(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            hook = tmp_path / ".kuhbs/my-kuhbs/signal/hooks/tpl/update.sh"
            hook.parent.mkdir(parents=True)
            hook.write_text("echo template update\n", encoding="utf-8")
            runner = UpgradeRunner(base_ctx.logger, existing={"tpl-signal", "app-signal"})
            ctx = UpgradeContext(base_ctx.defaults, base_ctx.logger, runner)

            upgrade_batch.run_targets(ctx, [definition])

            copies = [command for command in runner.commands if command[:2] == ["qvm-copy-to-vm", "tpl-signal"]]
            copied_scripts = [Path(path).name for command in copies for path in command[2:]]
            self.assertLess(copied_scripts.index("apt-update.sh"), copied_scripts.index("update.sh"))
            self.assertFalse(any("apt-get" in command for command in runner.commands))
            terminals = [command for command in runner.commands if command[:5] == ["qvm-run", "--quiet", "--user", "root", "tpl-signal"]]
            self.assertTrue(any("/home/user/QubesIncoming/dom0/apt-update.sh" in " ".join(command) for command in terminals))

    def test_shared_apt_script_cites_and_matches_qubes_updater(self):
        script = Path(__file__).resolve().parents[1] / "install/templates/usr/share/kuhbs/setup-scripts/kuhbs/setup/apt-update.sh"
        text = script.read_text(encoding="utf-8")

        self.assertIn("intentionally performs APT updates exactly like Qubes OS", text)
        self.assertIn("QubesOS/qubes-core-admin-linux/blob/582536ccd95d759c82e189102a50404b8c41d114/vmupdate/agent/source/apt/apt_cli.py", text)
        expected = [
            "export DEBIAN_FRONTEND=noninteractive",
            "fcntl.lockf",
            "Debug::NoLocking=true",
            "APT::Update::Error-Mode=any",
            "Dpkg::Options::=--force-confdef",
            "Dpkg::Options::=--force-confold",
            "dist-upgrade",
            "apt-get autoremove -s",
            "apt-get remove -y",
            "/usr/lib/qubes/upgrades-status-notify || true",
            "apt-get clean",
        ]
        positions = [text.index(fragment) for fragment in expected]
        self.assertEqual(positions, sorted(positions))

    def test_upgrade_runs_app_update_hook_and_prompts_before_restart(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            hook = tmp_path / ".kuhbs/my-kuhbs/signal/hooks/app/update.sh"
            hook.parent.mkdir(parents=True)
            hook.write_text("#!/bin/bash\necho app update\n")
            runner = UpgradeRunner(base_ctx.logger, existing={"tpl-signal", "app-signal"}, running={"app-signal"})
            ctx = UpgradeContext(base_ctx.defaults, base_ctx.logger, runner)

            with InputPatch("y"):
                upgrade_batch.run_targets(ctx, [definition])

            # The app hook is the reason to touch the AppVM; the restart happens only after one prompt
            self.assertIn(["qvm-copy-to-vm", "app-signal"], [cmd[:2] for cmd in runner.commands if cmd[:1] == ["qvm-copy-to-vm"]])
            self.assertIn(["qvm-shutdown", "--wait", "--force", "app-signal"], runner.commands)
            self.assertIn(["qvm-start", "app-signal"], runner.commands)
            self.assertTrue((tmp_path / ".kuhbs/upgrades/tpl-signal").exists())
            self.assertTrue((tmp_path / ".kuhbs/upgrades/app-signal").exists())

    def test_upgrade_app_update_hook_restarts_running_named_disposable_dependent(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            definition["type"] = "ndp"
            definition["kuhs"] = {"tpl": {}, "app": {"instances": [{"id": "default"}]}, "ndp": {"instances": [{"id": "default"}]}}
            definition = merge_kuhb_definition(base_ctx.defaults, definition)
            hook = tmp_path / ".kuhbs/my-kuhbs/signal/hooks/app/update.sh"
            hook.parent.mkdir(parents=True)
            hook.write_text("echo app update\n")
            runner = UpgradeRunner(
                base_ctx.logger,
                existing={"tpl-signal", "app-signal", "ndp-signal"},
                running={"ndp-signal"},
                prefs={
                    # Regression: app-signal changed by its update hook must count as a root
                    # for running named-disposable dependents, even when their chain does not
                    # include the package-upgraded tpl-signal.
                    ("ndp-signal", "klass"): "AppVM",
                    ("ndp-signal", "dispvm_template"): "app-signal",
                },
            )
            ctx = UpgradeContext(base_ctx.defaults, base_ctx.logger, runner)

            with InputPatch("") as prompt:
                upgrade_batch.run_targets(ctx, [definition])

            prompt_text = prompt.stdout.getvalue()
            self.assertIn("ndp-signal", prompt_text)
            self.assertIn("Would you like to restart them now? [Y/n]", prompt_text)
            self.assertIn(["qvm-shutdown", "--wait", "--force", "ndp-signal"], runner.commands)
            self.assertIn(["qvm-start", "ndp-signal"], runner.commands)

    def test_upgrade_restarts_named_dispvm_dependents(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            runner = UpgradeRunner(
                base_ctx.logger,
                existing={"tpl-signal", "ndp-signal"},
                running={"ndp-signal"},
                prefs={
                    # Named DispVM NDPs are persistent kuhs, not anonymous runtime disp1234 throwaways
                    ("ndp-signal", "klass"): "DispVM",
                    ("ndp-signal", "dispvm_template"): "app-signal",
                    ("ndp-signal", "template"): "tpl-signal",
                },
            )
            ctx = UpgradeContext(base_ctx.defaults, base_ctx.logger, runner)

            with InputPatch("") as prompt:
                upgrade_batch.run_targets(ctx, [definition])

            prompt_text = prompt.stdout.getvalue()
            self.assertIn("The following kuhs were running before the upgrade started", prompt_text)
            self.assertIn("ndp-signal", prompt_text)
            self.assertNotIn("The following unnamed disposable kuhs will just be shut down:\nndp-signal", prompt_text)
            self.assertIn(["qvm-shutdown", "--wait", "--force", "ndp-signal"], runner.commands)
            self.assertIn(["qvm-start", "ndp-signal"], runner.commands)

    def test_upgrade_restarts_template_dependents_and_only_stops_runtime_dispvms(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            runner = UpgradeRunner(
                base_ctx.logger,
                existing={"tpl-signal", "app-signal"},
                running={"app-signal", "disp1234"},
                prefs={
                    ("app-signal", "klass"): "AppVM",
                    ("app-signal", "template"): "tpl-signal",
                    ("disp1234", "klass"): "DispVM",
                    ("disp1234", "dispvm_template"): "app-signal",
                    ("disp1234", "template"): "tpl-signal",
                },
            )
            ctx = UpgradeContext(base_ctx.defaults, base_ctx.logger, runner)

            with InputPatch("") as prompt:
                upgrade_batch.run_targets(ctx, [definition])

            # Template upgrades affect running dependents; runtime disposables are stopped but not recreated
            prompt_text = prompt.stdout.getvalue()
            self.assertIn("The following kuhs were running before the upgrade started", prompt_text)
            self.assertIn("app-signal", prompt_text)
            self.assertIn("The following unnamed disposable kuhs will just be shut down:", prompt_text)
            self.assertIn("disp1234", prompt_text)
            self.assertIn("Mind that running applications are not restarted automatically.", prompt_text)
            self.assertIn("Would you like to restart them now? [Y/n]", prompt_text)
            self.assertIn(["qvm-shutdown", "--wait", "--force", "app-signal"], runner.commands)
            self.assertIn(["qvm-start", "app-signal"], runner.commands)
            self.assertIn(["qvm-shutdown", "--wait", "--force", "disp1234"], runner.commands)
            self.assertNotIn(["qvm-start", "disp1234"], runner.commands)
            self.assertIn("disp1234 was stopped; relaunch the app if needed", (tmp_path / "kuhbs.log").read_text())

    def test_upgrade_uses_qubes_class_not_name_prefix_for_dispvm_detection(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            runner = UpgradeRunner(
                base_ctx.logger,
                existing={"tpl-signal"},
                running={"disp-work"},
                prefs={
                    # Names are user-controlled; only the Qubes class marks runtime DisposableVMs
                    ("disp-work", "klass"): "AppVM",
                    ("disp-work", "template"): "tpl-signal",
                },
            )
            ctx = UpgradeContext(base_ctx.defaults, base_ctx.logger, runner)

            with InputPatch("") as prompt:
                upgrade_batch.run_targets(ctx, [definition])

            prompt_text = prompt.stdout.getvalue()
            self.assertIn("disp-work", prompt_text)
            self.assertNotIn("disp-work: shutdown required", prompt_text)
            self.assertIn(["qvm-shutdown", "--wait", "--force", "disp-work"], runner.commands)
            self.assertIn(["qvm-start", "disp-work"], runner.commands)

    def test_upgrade_declined_restart_is_not_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            base_ctx, definition = context(tmp_path)
            runner = UpgradeRunner(
                base_ctx.logger,
                existing={"tpl-signal", "app-signal"},
                running={"app-signal"},
                prefs={("app-signal", "klass"): "AppVM", ("app-signal", "template"): "tpl-signal"},
            )
            ctx = UpgradeContext(base_ctx.defaults, base_ctx.logger, runner)

            with InputPatch("n"):
                upgrade_batch.run_targets(ctx, [definition])

            # Declining is an accepted choice, so running VMs stay untouched without a failure-looking warning
            self.assertNotIn(["qvm-shutdown", "--wait", "--force", "app-signal"], runner.commands)
            self.assertNotIn(["qvm-start", "app-signal"], runner.commands)
            self.assertNotIn("may still use old template/app state", (tmp_path / "kuhbs.log").read_text())

    def test_terminal_spawns_terminal_and_returns_after_qvm_run_launch(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx, _definition = context(tmp_path)
            mark_created(ctx, "signal")

            terminal.run(ctx, "app-signal")

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("qvm-run --quiet app-signal test -x /usr/bin/xfce4-terminal", log_text)
            self.assertIn("qvm-run --quiet --user user app-signal /usr/bin/xfce4-terminal", log_text)
            self.assertIn("/usr/bin/xfce4-terminal --title", log_text)
            self.assertIn("app-signal: Terminal", log_text)

if __name__ == "__main__":
    unittest.main()
