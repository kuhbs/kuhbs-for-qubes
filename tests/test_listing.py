# Purpose: Regression tests for dom0-only kuhbs list rendering
# Scope: Fake Qubes probes keep status output deterministic
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from kuhbs.command import CommandResult, CommandRunner
from kuhbs.config import load_kuhb_definition
from kuhbs.listing import LABEL_COLORS, collect_status, render_status
from helpers import RecordingEventLogger as EventLogger
from kuhbs.model import merge_kuhb_definition
from kuhbs.operations import OperationContext
from kuhbs.qubes_status import QubesStatus
from kuhbs.repos import local_kuhb_paths
from helpers import RunnerContext, current_defaults


def defaults(tmp_path: Path) -> dict:
    # Tests use an isolated KUHBS tree so state/backup inference never touches the real home directory
    return current_defaults({
        "paths": {
            "config": str(tmp_path / ".kuhbs"),
            "kuhbs": str(tmp_path / ".kuhbs/my-kuhbs"),
            "desktop_applications": str(tmp_path / ".local/share/applications"),
            "scripts": str(tmp_path / ".kuhbs/scripts"),
            "setup_scripts": str(tmp_path / "usr/share/kuhbs/setup-scripts"),
            "user_setup_scripts": str(tmp_path / ".kuhbs/setup-scripts"),
            "qubes_xml": str(tmp_path / "qubes.xml"),
        },
        "backup": {"kuh": "ndp-kuhbs-usb", "crypt_name": "kuhbs-backups", "max_age_hours": 24, "ignore_failed_read": True, "dom0_paths": ["/home/user/*"]},
        "upgrade": {"max_age_minutes": 720},
        "terminal": {"path": "/usr/bin/xfce4-terminal", "args": [], "fallback": {"path": "/usr/bin/xterm", "args": []}},
        "prefs": {"app": {"autostart": False}, "tpl": {}, "ndp": {"autostart": False}, "sta": {"autostart": False}},
        "services": {"app": {}, "tpl": {}, "ndp": {}, "sta": {"instances": [{"id": "default"}]}},
        "features": {"app": {}, "tpl": {}, "ndp": {}, "sta": {"instances": [{"id": "default"}]}},
    })


def local_definitions(tmp_path: Path, configured_defaults: dict) -> list[dict]:
    # Tests explicitly use the same load-once and merge-once boundary as startup validation
    return [
        merge_kuhb_definition(configured_defaults, load_kuhb_definition(path))
        for path in local_kuhb_paths(tmp_path / ".kuhbs/my-kuhbs")
    ]


def write_qubes_xml(tmp_path: Path, domains: list[tuple[str, str, str, str, str | None]]) -> None:
    rows = []
    for name, klass, label, template, netvm in domains:
        props = [f'<property name="label">{label}</property>', f'<property name="name">{name}</property>']
        if template:
            props.append(f'<property name="template">{template}</property>')
        if netvm is not None:
            props.append(f'<property name="netvm">{netvm}</property>')
        rows.append(f'<domain class="{klass}"><properties>{"".join(props)}</properties></domain>')
    (tmp_path / "qubes.xml").write_text(
        '<qubes><properties><property name="default_netvm">sys-firewall</property></properties><domains>'
        '<domain class="AdminVM"><properties><property name="label">black</property></properties></domain>'
        + "".join(rows)
        + '</domains></qubes>',
        encoding="utf-8",
    )


def write_signal_qubes_xml(tmp_path: Path) -> None:
    write_qubes_xml(
        tmp_path,
        [
            ("app-signal", "AppVM", "green", "tpl-signal", None),
            ("tpl-signal", "TemplateVM", "purple", "", None),
            ("sta-signal", "StandaloneVM", "blue", "", ""),
            ("sys-firewall", "DispVM", "green", "default-dvm", "sys-net"),
            ("sys-net", "AppVM", "red", "fedora", ""),
        ],
    )


def write_ref_qubes_xml(tmp_path: Path) -> None:
    (tmp_path / "qubes.xml").write_text(
        '<qubes>'
        '<labels>'
        '<label id="label-red" name="red"/>'
        '<label id="label-green" name="green"/>'
        '<label id="label-purple" name="purple"/>'
        '</labels>'
        '<properties><property name="default_netvm" ref="domain-firewall"/></properties>'
        '<domains>'
        '<domain id="domain-app" class="AppVM"><properties>'
        '<property name="name">app-signal</property>'
        '<property name="label" ref="label-green"/>'
        '<property name="template" ref="domain-template"/>'
        '</properties></domain>'
        '<domain id="domain-template" class="TemplateVM"><properties>'
        '<property name="name">tpl-signal</property>'
        '<property name="label" ref="label-purple"/>'
        '</properties></domain>'
        '<domain id="domain-firewall" class="AppVM"><properties>'
        '<property name="name">sys-firewall</property>'
        '<property name="label" ref="label-green"/>'
        '<property name="netvm" ref="domain-net"/>'
        '</properties></domain>'
        '<domain id="domain-net" class="AppVM"><properties>'
        '<property name="name">sys-net</property>'
        '<property name="label" ref="label-red"/>'
        '<property name="netvm"></property>'
        '</properties></domain>'
        '</domains>'
        '</qubes>',
        encoding="utf-8",
    )


class ListingRunner(CommandRunner):
    # Fake the exact dom0 probes used by listing so regressions show up as missing expected output
    def __init__(self, logger):
        super().__init__(logger)
        self.commands: list[list[str]] = []

    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        self.commands.append(list(args))
        command = tuple(args)
        stdout = ""
        returncode = 0
        if command == ("xl", "list"):
            # Keep every Xen state in the shared fixture so transient states cannot silently become running
            stdout = (
                "Name ID Mem VCPUs State Time(s)\n"
                "Domain-0 0 1 1 r----- 1.0\n"
                "app-signal 1 1 1 -b---- 1.0\n"
                "ndp-kuhbs-usb 2 1 1 -b---- 1.0\n"
                "sys-firewall 3 1 1 -b---- 1.0\n"
                "sys-net 4 1 1 -b---- 1.0\n"
                "paused-test 5 1 1 --p--- 1.0\n"
                "shutdown-test 6 1 1 ---s-- 1.0\n"
                "crashed-test 7 1 1 ----c- 1.0\n"
                "dying-test 8 1 1 -----d 1.0\n"
            )
        elif command in {
            ("qvm-check", "--quiet", "app-signal"),
            ("qvm-check", "--quiet", "tpl-signal"),
            ("qvm-check", "--quiet", "sta-signal"),
        }:
            returncode = 0
        elif command == ("qvm-check", "--quiet", "--running", "app-signal"):
            returncode = 0
        elif command == ("qvm-check", "--quiet", "--running", "ndp-kuhbs-usb"):
            # Backup archive probes are admitted only after the passive running prerequisite
            returncode = 0
        elif command in {
            ("qvm-check", "--quiet", "--running", "tpl-signal"),
            ("qvm-check", "--quiet", "--running", "sta-signal"),
        }:
            returncode = 1
        elif command == ("qvm-prefs", "app-signal", "label"):
            stdout = "green\n"
        elif command == ("qvm-prefs", "tpl-signal", "label"):
            stdout = "purple\n"
        elif command == ("qvm-prefs", "sta-signal", "label"):
            stdout = "blue\n"
        elif command == ("qvm-prefs", "app-signal", "netvm"):
            # Netvm chains are rendered as a tree below the compact table
            stdout = "sys-firewall\n"
        elif command == ("qvm-prefs", "sys-firewall", "netvm"):
            stdout = "sys-net\n"
        elif command in {
            ("qvm-prefs", "sys-net", "netvm"),
            ("qvm-prefs", "tpl-signal", "netvm"),
            ("qvm-prefs", "sta-signal", "netvm"),
        }:
            stdout = "None\n"
        elif command in {
            (
                "qvm-run",
                "--quiet",
                "--user",
                "root",
                "ndp-kuhbs-usb",
                "/usr/bin/mountpoint",
                "--quiet",
                "/mnt",
            ),
            (
                "qvm-run",
                "--quiet",
                "--user",
                "root",
                "ndp-kuhbs-usb",
                "/usr/bin/test",
                "-d",
                "/mnt/kuhbs-backup",
            ),
        }:
            returncode = 0
        elif command == ("qvm-run", "--quiet", "--user", "root", "ndp-kuhbs-usb", "sh", "-c", "test -f /mnt/kuhbs-backup/app-signal.tar.zst"):
            returncode = 0
        elif command == ("qvm-run", "--quiet", "--user", "root", "ndp-kuhbs-usb", "sh", "-c", "test -n \"$(find /mnt/kuhbs-backup/app-signal.tar.zst -maxdepth 0 -type f -mmin -1440 -print -quit)\""):
            returncode = 0
        elif command == ("qvm-run", "--quiet", "--user", "root", "ndp-kuhbs-usb", "sh", "-c", "test -n \"$(find /mnt/kuhbs-backup/app-signal.tar.zst -maxdepth 0 -type f -mmin -0.00048 -print -quit)\""):
            # Fractional-hour thresholds must survive conversion to find's minute unit
            returncode = 0
        else:
            returncode = 1
        return CommandResult(returncode=returncode, stdout=stdout)


class ListingContext(OperationContext):
    @property
    def runner(self):
        # Inject the fake runner through the same context property the CLI uses
        return ListingRunner(self.logger)


class HaltedBackupListingRunner(ListingRunner):
    # A halted backup VM is absent from xl even though its persistent Qubes definition still exists
    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        if tuple(args) == ("xl", "list"):
            self.commands.append(list(args))
            stdout = "Name ID Mem VCPUs State Time(s)\nDomain-0 0 1 1 r----- 1.0\napp-signal 1 1 1 -b---- 1.0\n"
            return CommandResult(returncode=0, stdout=stdout)
        return super().run(args, source=source, check=check, visible_output=visible_output, log=log)


class StaleBackupMountListingRunner(ListingRunner):
    # A leftover directory must not make unmounted backup media look available
    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        command = tuple(args)
        if command == (
            "qvm-run",
            "--quiet",
            "--user",
            "root",
            "ndp-kuhbs-usb",
            "/usr/bin/mountpoint",
            "--quiet",
            "/mnt",
        ):
            return CommandResult(returncode=1)
        if command == (
            "qvm-run",
            "--quiet",
            "--user",
            "root",
            "ndp-kuhbs-usb",
            "/usr/bin/test",
            "-d",
            "/mnt/kuhbs-backup",
        ):
            return CommandResult(returncode=0)
        return super().run(
            args,
            source=source,
            check=check,
            visible_output=visible_output,
            log=log,
        )


class StaleBackupMountListingContext(OperationContext):
    @property
    def runner(self):
        return StaleBackupMountListingRunner(self.logger)


class PartialListingRunner(ListingRunner):
    # Only two of three expected kuhs exist, matching an interrupted create flow
    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        if tuple(args) == ("xl", "list"):
            stdout = "Name ID Mem VCPUs State Time(s)\napp-element-work 1 1 1 -b---- 1.0\n"
            return CommandResult(returncode=0, stdout=stdout)
        return super().run(args, source=source, check=check, visible_output=visible_output, log=log)


class PartialListingContext(OperationContext):
    @property
    def runner(self):
        # Keep one missing expected app so list must mark the kuhb broken, not created
        return PartialListingRunner(self.logger)


class ListingTests(unittest.TestCase):

    def test_xl_failure_aborts_status_collection(self):
        class FailedXlRunner:
            def run(self, _args, *, check=True, **_kwargs):
                if check:
                    raise RuntimeError("xl list failed")
                return CommandResult(returncode=1, stdout="")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ctx = RunnerContext(defaults(root), EventLogger(root / "kuhbs.log", stdout=False), FailedXlRunner())

            with self.assertRaisesRegex(RuntimeError, "xl list failed"):
                QubesStatus.load(ctx)

    def test_xen_runtime_flags_map_to_distinct_states(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            logger = EventLogger(tmp_path / "kuhbs.log", color=False, stdout=False)
            runner = ListingRunner(logger)
            ctx = RunnerContext(defaults(tmp_path), logger, runner)
            write_qubes_xml(tmp_path, [])

            states = QubesStatus.load(ctx).states

            # Blocked domains are active, while each exceptional Xen flag keeps its own status
            self.assertEqual(
                {
                    "dom0": "running",
                    "app-signal": "running",
                    "paused-test": "paused",
                    "shutdown-test": "shutdown",
                    "crashed-test": "crashed",
                    "dying-test": "dying",
                },
                {name: states[name] for name in (
                    "dom0",
                    "app-signal",
                    "paused-test",
                    "shutdown-test",
                    "crashed-test",
                    "dying-test",
                )},
            )

    def test_public_qubes_xml_parser_returns_static_reference_resolved_metadata(self):
        import kuhbs.qubes_status as qubes_status

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_ref_qubes_xml(root)
            parser = getattr(qubes_status, "read_qubes_xml", None)

            self.assertTrue(callable(parser))
            vms = parser(root / "qubes.xml")

        self.assertEqual(vms["app-signal"].label, "green")
        self.assertEqual(vms["app-signal"].template, "tpl-signal")
        self.assertEqual(vms["sys-firewall"].netvm, "sys-net")
        self.assertFalse(hasattr(vms["app-signal"], "state"))

    def test_halted_backup_vm_is_not_started_by_status_collection(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            write_signal_qubes_xml(tmp_path)
            configured_defaults = defaults(tmp_path)
            definition = merge_kuhb_definition(configured_defaults, {
                "id": "signal",
                "name": "Signal",
                "description": "Test",
                "icon": "icon.svg",
                "order": 500,
                "type": "app",
                "template": "debian-13-minimal",
                "kuhs": {"app": {"instances": [{"id": "default", "backup": {"paths": ["/home/user/data"]}}]}},
            })
            logger = EventLogger(tmp_path / "kuhbs.log", color=False, stdout=False)
            runner = HaltedBackupListingRunner(logger)
            ctx = RunnerContext(configured_defaults, logger, runner)

            statuses = collect_status(ctx, [definition])

            app = next(kuh for kuh in statuses[0].kuhs if kuh.name == "app-signal")
            self.assertEqual(app.backup, "unavailable")
            self.assertIn("unavailable", render_status(statuses))
            self.assertFalse(any(command[:4] == ["qvm-run", "--quiet", "--user", "root"] and "ndp-kuhbs-usb" in command for command in runner.commands))

    def test_unmounted_storage_does_not_report_stale_archive_directory(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            write_signal_qubes_xml(tmp_path)
            definition = {
                "id": "signal",
                "name": "Signal",
                "description": "Test",
                "icon": "icon.svg",
                "order": 500,
                "type": "app",
                "template": "debian-13-minimal",
                "kuhs": {
                    "app": {
                        "instances": [
                            {
                                "id": "default",
                                "backup": {"paths": ["/home/user/data"]},
                            },
                        ],
                    },
                },
            }
            ctx = StaleBackupMountListingContext(
                defaults(tmp_path),
                EventLogger(tmp_path / "kuhbs.log", color=False, stdout=False),
            )
            definition = merge_kuhb_definition(ctx.defaults, definition)

            statuses = collect_status(ctx, [definition])

            app = next(kuh for kuh in statuses[0].kuhs if kuh.name == "app-signal")
            self.assertEqual(app.backup, "unavailable")
            self.assertIn("unavailable", render_status(statuses))

    def test_renders_selected_dom0_status_fields(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            write_signal_qubes_xml(tmp_path)
            kuhb_dir = tmp_path / ".kuhbs/my-kuhbs/signal"
            kuhb_dir.mkdir(parents=True)
            # Minimal local definition is enough; listing derives expected VM names from kuhb schema
            (kuhb_dir / "kuhb.yml").write_text(
                "# Test fixture: listing derives expected qubes from this local kuhb definition\n"
                "id: signal\n"
                "name: Signal\n"
                "description: Test\n"
                "icon: icon.svg\n"
                "order: 500\n"
                "type: app\n"
                "# Top-level template is required even though list does not create anything\n"
                "template: debian-13-minimal\n"
                "# Include template, app, and standalone rows to exercise update and volume rendering\n"
                "kuhs:\n"
                "  tpl: {}\n"
                "  app:\n"
                "    instances:\n"
                "      - id: default\n"
                "        prefs:\n"
                "          label: green\n"
                "        backup:\n"
                "          paths:\n"
                "            - /home/user/.config/Signal\n"
                "  sta:\n"
                "    instances:\n"
                "      - id: default\n"
                "        prefs:\n"
                "          label: blue\n",
                encoding="utf-8",
            )
            # A second local definition stays absent so missing rows can still use intended YAML labels
            play_dir = tmp_path / ".kuhbs/my-kuhbs/play"
            play_dir.mkdir(parents=True)
            (play_dir / "kuhb.yml").write_text(
                "# Test fixture: absent kuhbs should still render intended Qubes label colors\n"
                "id: play\n"
                "name: Play\n"
                "description: Test\n"
                "icon: icon.svg\n"
                "order: 500\n"
                "type: sta\n"
                "template: debian-13-minimal\n"
                "kuhs:\n"
                "  sta:\n"
                "    # Explicit label is available even before the VM exists\n"
                "    instances:\n"
                "      - id: default\n"
                "        prefs:\n"
                "          label: yellow\n"
                "        backup:\n"
                "          paths:\n"
                "            - /home/user/data\n",
                encoding="utf-8",
            )
            upgrade_dir = tmp_path / ".kuhbs/upgrades"
            upgrade_dir.mkdir(parents=True)
            (upgrade_dir / "tpl-signal").write_text(f"{datetime.now().astimezone().isoformat(timespec='seconds')}\n")
            stale = datetime.now().astimezone() - timedelta(minutes=800)
            (upgrade_dir / "sta-signal").write_text(f"{stale.isoformat(timespec='seconds')}\n")
            ctx = ListingContext(defaults(tmp_path), EventLogger(tmp_path / "kuhbs.log", color=False, stdout=False))

            statuses = collect_status(ctx, local_definitions(tmp_path, ctx.defaults))
            output = render_status(statuses)

            # A missing state file should not make fully existing Qubes resources display as absent
            self.assertNotIn("KUHBS", output)
            self.assertNotIn("Signal:", output)
            self.assertIn("KUHB/KUH", output)
            self.assertIn("Upgrade", output)
            self.assertIn("signal", output)
            self.assertIn("Linked", output)
            self.assertNotIn("signal: Linked", output)
            self.assertIn("app-signal", output)
            self.assertIn("running", output)
            self.assertIn("<24h", output)
            self.assertIn("upgraded:0m", output)
            self.assertIn("upgradable:13h", output)
            self.assertNotIn("app-signal  running  up", output)
            self.assertNotIn("PRIVATE", output)
            self.assertNotIn("ROOT", output)
            self.assertNotIn("RAM", output)
            self.assertIn("tpl-signal", output)
            self.assertIn("halted", output)
            self.assertIn("play", output)
            self.assertIn("absent", output)
            self.assertNotIn("play: absent", output)
            play = next(status for status in statuses if status.kuhb_id == "play")
            self.assertEqual("absent", play.kuhs[0].status)
            self.assertEqual("yellow", play.kuhs[0].label)
            self.assertIn("Network chain\nsys-net\n└── sys-firewall\n    └── app-signal", output)
            self.assertIn("No network kuh: sta-signal, tpl-signal", output)
            self.assertNotIn("none\n", output)
            self.assertNotIn("sta-play", output.split("Network chain", 1)[1])
            self.assertTrue(output.startswith("KUHB/KUH"))

    def test_fractional_backup_age_uses_fractional_minutes_and_readable_seconds(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            write_signal_qubes_xml(tmp_path)
            current_defaults = defaults(tmp_path)
            # Tiny development thresholds must not be rounded up to a hidden one-minute window
            current_defaults["backup"]["max_age_hours"] = 0.000008
            kuhb_dir = tmp_path / ".kuhbs/my-kuhbs/signal"
            kuhb_dir.mkdir(parents=True)
            (kuhb_dir / "kuhb.yml").write_text(
                "# Test fixture: backup-enabled app row exercises fractional age rendering\n"
                "id: signal\n"
                "name: Signal\n"
                "description: Test\n"
                "icon: icon.svg\n"
                "order: 500\n"
                "type: sta\n"
                "template: debian-13-minimal\n"
                "kuhs:\n"
                "  app:\n"
                "    instances:\n"
                "      - id: default\n"
                "        prefs:\n"
                "          label: green\n"
                "        backup:\n"
                "          paths:\n"
                "            - /home/user/.config/Signal\n",
                encoding="utf-8",
            )
            ctx = ListingContext(current_defaults, EventLogger(tmp_path / "kuhbs.log", color=False, stdout=False))

            output = render_status(collect_status(ctx, local_definitions(tmp_path, ctx.defaults)))

            self.assertIn("<0s", output)
            self.assertNotIn("8e-06h", output)
            self.assertNotIn("0.03s", output)

    def test_partial_created_kuhb_renders_broken(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            write_qubes_xml(
                tmp_path,
                [
                    ("tpl-element", "TemplateVM", "purple", "", None),
                    ("app-element-work", "AppVM", "green", "tpl-element", ""),
                ],
            )
            kuhb_dir = tmp_path / ".kuhbs/my-kuhbs/element"
            kuhb_dir.mkdir(parents=True)
            (kuhb_dir / "kuhb.yml").write_text(
                "# Test fixture: partial resources should not look created or absent\n"
                "id: element\n"
                "name: Element\n"
                "description: Test\n"
                "icon: icon.svg\n"
                "order: 500\n"
                "type: sta\n"
                "template: debian-13-minimal\n"
                "kuhs:\n"
                "  tpl: {}\n"
                "  app:\n"
                "    instances:\n"
                "      - id: work\n"
                "        prefs:\n"
                "          label: green\n"
                "      - id: private\n"
                "        prefs:\n"
                "          label: yellow\n",
                encoding="utf-8",
            )
            ctx = PartialListingContext(defaults(tmp_path), EventLogger(tmp_path / "kuhbs.log", color=False, stdout=False))

            statuses = collect_status(ctx, local_definitions(tmp_path, ctx.defaults))
            output = render_status(statuses)

            element = statuses[0]
            self.assertEqual("linked", element.state)
            self.assertIn("element", output)
            self.assertIn("Linked", output)
            self.assertNotIn("element: broken", output)
            self.assertIn("app-element-private", output)
            self.assertIn("absent", output)

    def test_qubes_xml_ref_properties_resolve_labels_and_netvm_chain(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            write_ref_qubes_xml(tmp_path)
            kuhb_dir = tmp_path / ".kuhbs/my-kuhbs/signal"
            kuhb_dir.mkdir(parents=True)
            (kuhb_dir / "kuhb.yml").write_text(
                "id: signal\n"
                "name: Signal\n"
                "description: Test\n"
                "icon: icon.svg\n"
                "order: 500\n"
                "type: app\n"
                "template: debian-13-minimal\n"
                "kuhs:\n"
                "  tpl: {}\n"
                "  app:\n"
                "    instances:\n"
                "      - id: default\n"
                "        prefs:\n"
                "          label: green\n",
                encoding="utf-8",
            )
            ctx = ListingContext(defaults(tmp_path), EventLogger(tmp_path / "kuhbs.log", color=False, stdout=False))

            statuses = collect_status(ctx, local_definitions(tmp_path, ctx.defaults))
            output = render_status(statuses)

            signal = statuses[0]
            app = next(kuh for kuh in signal.kuhs if kuh.name == "app-signal")
            tpl = next(kuh for kuh in signal.kuhs if kuh.name == "tpl-signal")
            self.assertEqual("green", app.label)
            self.assertEqual("purple", tpl.label)
            self.assertEqual(("sys-firewall", "sys-net"), app.net_chain)
            self.assertIn("Network chain\nsys-net\n└── sys-firewall\n    └── app-signal", output)

    def test_blue_label_uses_light_ansi_color_for_dark_terminals(self):
        # Dark ANSI blue is unreadable on the user's terminal background; use bright 256-color blue
        self.assertEqual("\033[38;5;39m", LABEL_COLORS["blue"])

    def test_gray_uses_brighter_ansi_color_for_halted_rows(self):
        # Plain ANSI bright-black is still too dim on the user's dark background
        self.assertEqual("\033[38;5;245m", LABEL_COLORS["gray"])


if __name__ == "__main__":
    unittest.main()
