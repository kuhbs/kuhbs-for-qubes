# Purpose: Regression tests for Qubes Snitch starter rules
# Scope: Uses fake Qubes commands instead of touching dom0 or AppVMs
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kuhbs.command import CommandResult, CommandRunner
from helpers import RecordingEventLogger as EventLogger
from kuhbs.model import merge_kuhb_definition
from kuhbs.operations import OperationContext, create, remove
from helpers import current_defaults


def make_defaults(tmp_path):
    runner = tmp_path / "usr/share/kuhbs/setup-scripts/kuhbs/kuhbs-run-script.sh"
    runner.parent.mkdir(parents=True)
    runner.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    runner.chmod(0o700)
    return current_defaults({
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
        "firewall": {
            "qubes_snitch": {
                "snitch_vm": "app-kuhbs-net-firewall",
                "rules_dir": "/rw/usrlocal/qubes-snitch/rules",
            },
        },
        "upgrade": {"max_age_minutes": 720, "restart_without_prompt": []},
        "terminal": {"path": "/usr/bin/xfce4-terminal", "args": [], "fallback": {"path": "/usr/bin/xterm", "args": []}},
        "prefs": {"app": {"autostart": False}, "tpl": {"label": "purple"}, "ndp": {"autostart": False}, "sta": {"autostart": False}},
        "services": {"app": {}, "tpl": {}, "ndp": {}, "sta": {"instances": [{"id": "default"}]}},
        "features": {"app": {}, "tpl": {}, "ndp": {}, "sta": {"instances": [{"id": "default"}]}},
    })


def make_definition(tmp_path):
    kuhb_dir = tmp_path / ".kuhbs/my-kuhbs/browse"
    (kuhb_dir / "snitch-rules").mkdir(parents=True)
    (kuhb_dir / "snitch-rules/app-browse.yml").write_text("rules4: []\n\ndns: []\n", encoding="utf-8")
    return {
        "id": "browse",
        "name": "Browse",
        "description": "Browser kuhb",
        "icon": "icon.svg",
        "order": 500,
        "type": "app",

        "template": "debian-13-minimal",
        "kuhs": {"tpl": {}, "app": {"instances": [{"id": "default", "prefs": {"label": "orange"}}]}},
    }


def make_remove_definition(defaults, kuhb_id, *, all_kinds=False, order=500):
    # The full fixture proves cleanup covers tpl/app/ndp/sta plus the app's stable disposable policy
    kuhb_type = "ndp" if all_kinds else "app"
    kuhs = {"app": {"instances": [{"id": "default"}]}}
    if all_kinds:
        kuhs = {
            "tpl": {},
            "app": {"instances": [{"id": "default", "prefs": {"template_for_dispvms": True}}]},
            "ndp": {"instances": [{"id": "default"}]},
            "sta": {"instances": [{"id": "default"}]},
        }
    return merge_kuhb_definition(defaults, {
        "id": kuhb_id,
        "name": kuhb_id.title(),
        "description": f"{kuhb_id} removal fixture",
        "icon": "icon.svg",
        "order": order,
        "type": kuhb_type,
        "template": "debian-13-minimal",
        "kuhs": kuhs,
    })


def write_runtime_dispvm_xml(defaults, base_name, runtime_name, *, named_name=None):
    # Match Qubes' template links so Remove exercises the same relation resolver as Upgrade
    path = Path(defaults["paths"]["config"]).parent / "qubes.xml"
    defaults["paths"]["qubes_xml"] = str(path)
    named_domain = ""
    if named_name is not None:
        named_domain = (
            f'<domain class="DispVM"><properties><property name="name">{named_name}</property>'
            f'<property name="template">tpl-browse</property><property name="dispvm_template">{base_name}</property>'
            "</properties></domain>"
        )
    path.write_text(
        "<qubes><domains>"
        f'<domain class="AppVM"><properties><property name="name">{base_name}</property>'
        '<property name="template">tpl-browse</property></properties></domain>'
        f'<domain class="DispVM"><properties><property name="name">{runtime_name}</property>'
        f'<property name="template">tpl-browse</property><property name="dispvm_template">{base_name}</property>'
        "</properties></domain>"
        f"{named_domain}"
        "</domains></qubes>",
        encoding="utf-8",
    )


def write_runtime_dispvm_pairs_xml(defaults, pairs):
    # Multiple order groups need one shared Qubes snapshot with separate runtime descendants
    path = Path(defaults["paths"]["config"]).parent / "qubes.xml"
    defaults["paths"]["qubes_xml"] = str(path)
    domains = []
    for base_name, runtime_name in pairs:
        domains.append(
            f'<domain class="AppVM"><properties><property name="name">{base_name}</property>'
            '<property name="template">tpl-base</property></properties></domain>'
        )
        domains.append(
            f'<domain class="DispVM"><properties><property name="name">{runtime_name}</property>'
            f'<property name="template">tpl-base</property><property name="dispvm_template">{base_name}</property>'
            "</properties></domain>"
        )
    path.write_text(f"<qubes><domains>{''.join(domains)}</domains></qubes>", encoding="utf-8")


class SnitchRuleRunner(CommandRunner):
    def __init__(
        self,
        logger,
        *,
        firewall_exists=True,
        rule_exists=False,
        rule_files=None,
        managed_exists=False,
        managed_vms=None,
        running_vms=None,
        fail_remove_vms=None,
        events=None,
    ):
        super().__init__(logger)
        self.firewall_exists = firewall_exists
        self.rule_exists = rule_exists
        self.rule_files = set(rule_files) if rule_files is not None else None
        # Explicit VM sets let Remove tests model qvm-kill/qvm-remove state transitions
        self.managed_exists = managed_exists
        self.managed_vms = set(managed_vms) if managed_vms is not None else None
        self.running_vms = set(running_vms or ())
        self.fail_remove_vms = set(fail_remove_vms or ())
        self.events = events
        self.commands = []

    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        if log:
            self.logger.command(source, args)
        self.commands.append(args)
        returncode = 0
        stdout = ""
        if args[:2] == ["qvm-check", "--quiet"]:
            vm_name = args[-1]
            if "--running" in args:
                returncode = 0 if vm_name in self.running_vms else 1
            elif vm_name == "app-kuhbs-net-firewall":
                returncode = 0 if self.firewall_exists else 1
            elif self.managed_vms is not None:
                returncode = 0 if vm_name in self.managed_vms else 1
            else:
                returncode = 0 if self.managed_exists else 1
        elif args == ["xl", "list"]:
            # QubesStatus uses one Xen snapshot to discover related runtime disposables
            rows = ["Name ID Mem VCPUs State Time(s)"]
            rows.extend(f"{name} 1 1 1 -b---- 1.0" for name in sorted(self.running_vms))
            stdout = "\n".join(rows) + "\n"
        elif len(args) >= 3 and args[-3:-1] == ["test", "-f"] and args[-1].endswith(".yml"):
            rule_name = Path(args[-1]).name
            if self.rule_files is not None:
                returncode = 0 if rule_name in self.rule_files else 1
            else:
                # Legacy create tests expose starter rules only for AppVM targets
                returncode = 0 if rule_name.startswith("app-") and self.rule_exists else 1
        elif args and args[0] == "qvm-kill":
            self.running_vms.discard(args[-1])
            if self.events is not None:
                self.events.append(f"kill:{args[-1]}")
        elif args and args[0] == "qvm-remove":
            returncode = 1 if args[-1] in self.fail_remove_vms else 0
            if returncode == 0 and self.managed_vms is not None:
                self.managed_vms.discard(args[-1])
            if self.events is not None:
                self.events.append(f"remove:{args[-1]}")
        elif len(args) >= 3 and args[-3:-1] == ["rm", "-f"] and args[-1].endswith(".yml"):
            rule_name = Path(args[-1]).name
            if self.rule_files is not None:
                self.rule_files.discard(rule_name)
            if self.events is not None:
                self.events.append(f"rule-remove:{rule_name}")
        elif args[-3:] == ["systemctl", "restart", "qubes-snitchd.service"] and self.events is not None:
            self.events.append("snitch-restart")
        if log:
            self.logger.exit(source, returncode, " ".join(args))
        if check and returncode != 0:
            raise RuntimeError("unexpected checked command")
        return CommandResult(returncode=returncode, stdout=stdout)


class SnitchRuleContext(OperationContext):
    def __init__(self, defaults, logger, runner):
        super().__init__(defaults, logger)
        self._runner = runner

    @property
    def runner(self):
        return self._runner


class SnitchRuleTests(unittest.TestCase):
    def test_create_seeds_missing_snitch_rule_file_once(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            logger = EventLogger(tmp_path / "kuhbs.log", color=False, stdout=False)
            runner = SnitchRuleRunner(logger, rule_exists=False)
            ctx = SnitchRuleContext(make_defaults(tmp_path), logger, runner)

            create.run(ctx, merge_kuhb_definition(ctx.defaults, make_definition(tmp_path)))

            source = str(tmp_path / ".kuhbs/my-kuhbs/browse/snitch-rules/app-browse.yml")
            incoming = "/home/user/QubesIncoming/dom0/app-browse.yml"
            target = "/rw/usrlocal/qubes-snitch/rules/app-browse.yml"
            install = ["qvm-run", "--user", "root", "app-kuhbs-net-firewall", "install", "-o", "root", "-g", "root", "-m", "0644", "-v", incoming, target]
            cleanup = ["qvm-run", "--user", "root", "app-kuhbs-net-firewall", "rm", "-f", incoming]
            restart = ["qvm-run", "--user", "root", "app-kuhbs-net-firewall", "systemctl", "restart", "qubes-snitchd.service"]
            self.assertIn(["qvm-copy-to-vm", "app-kuhbs-net-firewall", source], runner.commands)
            self.assertIn(install, runner.commands)
            self.assertIn(cleanup, runner.commands)
            self.assertIn(restart, runner.commands)
            # The daemon must not reload until the root-owned rule is installed and staging is cleaned
            self.assertLess(runner.commands.index(install), runner.commands.index(cleanup))
            self.assertLess(runner.commands.index(cleanup), runner.commands.index(restart))

    def test_create_keeps_existing_snitch_rule_file(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            logger = EventLogger(tmp_path / "kuhbs.log", color=False, stdout=False)
            runner = SnitchRuleRunner(logger, rule_exists=True)
            ctx = SnitchRuleContext(make_defaults(tmp_path), logger, runner)

            create.run(ctx, merge_kuhb_definition(ctx.defaults, make_definition(tmp_path)))

            self.assertNotIn("qvm-copy-to-vm", [command[0] for command in runner.commands])
            self.assertIn("Keeping existing Qubes Snitch rules for app-browse", (tmp_path / "kuhbs.log").read_text(encoding="utf-8"))

    def test_remove_collects_questions_before_workers_and_deletes_rules_after_removal(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            defaults = make_defaults(tmp_path)
            # One worker gives the question and post-removal cleanup phases a deterministic order
            defaults["batch_size"]["remove"] = 1
            logger = EventLogger(tmp_path / "kuhbs.log", color=False, stdout=False)
            events = []
            managed = {"tpl-alpha", "app-alpha", "tpl-beta", "app-beta"}
            runner = SnitchRuleRunner(
                logger,
                rule_files={"app-alpha.yml", "app-beta.yml"},
                managed_vms=managed,
                events=events,
            )
            ctx = SnitchRuleContext(defaults, logger, runner)
            definitions = tuple(make_remove_definition(defaults, kuhb_id) for kuhb_id in ("alpha", "beta"))
            answers = iter(("y", "n"))

            def answer(prompt):
                # Prompts share the mutation log so no policy question can drift before VM removal
                events.append(f"prompt:{prompt}")
                return next(answers)

            with patch("builtins.input", side_effect=answer):
                remove.run_all(ctx, definitions)

            self.assertEqual(events, [
                "prompt:Remove Qubes Snitch rules for app-alpha? [y/n] ",
                "prompt:Remove Qubes Snitch rules for app-beta? [y/n] ",
                "remove:app-alpha",
                "remove:tpl-alpha",
                "remove:app-beta",
                "remove:tpl-beta",
                "rule-remove:app-alpha.yml",
                "snitch-restart",
            ])

    def test_remove_cleans_every_kuh_kind_and_stable_disposable_policy(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            defaults = make_defaults(tmp_path)
            logger = EventLogger(tmp_path / "kuhbs.log", color=False, stdout=False)
            events = []
            identities = {
                "tpl-browse.yml",
                "app-browse.yml",
                "ndp-browse.yml",
                "sta-browse.yml",
                "dispvm-app-browse.yml",
            }
            runner = SnitchRuleRunner(
                logger,
                rule_files=identities,
                managed_vms={name.removesuffix(".yml") for name in identities if not name.startswith("dispvm-")},
                events=events,
            )
            ctx = SnitchRuleContext(defaults, logger, runner)
            definition = make_remove_definition(defaults, "browse", all_kinds=True)

            def answer(prompt):
                # Record planned answers separately from the later policy deletion phase
                events.append(f"prompt:{prompt}")
                return "y"

            with patch("builtins.input", side_effect=answer):
                remove.run(ctx, definition)

            last_vm_remove = max(index for index, event in enumerate(events) if event.startswith("remove:"))
            first_rule_remove = min(index for index, event in enumerate(events) if event.startswith("rule-remove:"))
            self.assertLess(last_vm_remove, first_rule_remove)
            self.assertEqual(
                {event.removeprefix("rule-remove:") for event in events if event.startswith("rule-remove:")},
                identities,
            )
            self.assertEqual(events.count("snitch-restart"), 1)

    def test_remove_kills_related_runtime_disposable_before_configured_kuhs(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            defaults = make_defaults(tmp_path)
            write_runtime_dispvm_xml(defaults, "app-browse", "disp1234", named_name="disp-work")
            logger = EventLogger(tmp_path / "kuhbs.log", color=False, stdout=False)
            events = []
            runner = SnitchRuleRunner(
                logger,
                managed_vms={"tpl-browse", "app-browse"},
                running_vms={"disp1234", "disp-work"},
                events=events,
            )
            ctx = SnitchRuleContext(defaults, logger, runner)

            remove.run(ctx, make_remove_definition(defaults, "browse"))

            self.assertEqual(events[:3], [
                "kill:disp1234",
                "remove:app-browse",
                "remove:tpl-browse",
            ])
            self.assertNotIn("kill:disp-work", events)

    def test_remove_does_not_kill_later_order_udp_after_earlier_order_fails(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            defaults = make_defaults(tmp_path)
            defaults["batch_size"]["remove"] = 1
            write_runtime_dispvm_pairs_xml(
                defaults,
                (("app-alpha", "disp1001"), ("app-beta", "disp1002")),
            )
            logger = EventLogger(tmp_path / "kuhbs.log", color=False, stdout=False)
            events = []
            runner = SnitchRuleRunner(
                logger,
                rule_files=set(),
                managed_vms={"tpl-alpha", "app-alpha", "tpl-beta", "app-beta"},
                running_vms={"disp1001", "disp1002"},
                fail_remove_vms={"app-alpha"},
                events=events,
            )
            ctx = SnitchRuleContext(defaults, logger, runner)
            definitions = (
                make_remove_definition(defaults, "alpha", order=500),
                make_remove_definition(defaults, "beta", order=400),
            )

            with self.assertRaises(RuntimeError):
                remove.run_all(ctx, definitions)

            self.assertIn("kill:disp1001", events)
            self.assertNotIn("kill:disp1002", events)
            self.assertNotIn("remove:app-beta", events)

    def test_remove_prompt_interrupt_starts_no_vm_or_rule_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            defaults = make_defaults(tmp_path)
            defaults["batch_size"]["remove"] = 1
            logger = EventLogger(tmp_path / "kuhbs.log", color=False, stdout=False)
            events = []
            runner = SnitchRuleRunner(
                logger,
                rule_files={"app-alpha.yml", "app-beta.yml"},
                managed_vms={"tpl-alpha", "app-alpha", "tpl-beta", "app-beta"},
                events=events,
            )
            ctx = SnitchRuleContext(defaults, logger, runner)
            definitions = tuple(make_remove_definition(defaults, kuhb_id) for kuhb_id in ("alpha", "beta"))
            answers = iter(("y", KeyboardInterrupt()))

            def answer(prompt):
                # Collect every answer before deletion so interruption cannot apply only part of optional cleanup
                events.append(f"prompt:{prompt}")
                value = next(answers)
                if isinstance(value, BaseException):
                    raise value
                return value

            with patch("builtins.input", side_effect=answer):
                with self.assertRaises(KeyboardInterrupt):
                    remove.run_all(ctx, definitions)

            self.assertEqual(events, [
                "prompt:Remove Qubes Snitch rules for app-alpha? [y/n] ",
                "prompt:Remove Qubes Snitch rules for app-beta? [y/n] ",
            ])
            self.assertFalse(any(event.startswith("remove:") for event in events))
            self.assertFalse(any(event.startswith("rule-remove:") for event in events))
            self.assertNotIn("snitch-restart", events)
            self.assertFalse((tmp_path / ".kuhbs/states").exists())

    def test_remove_keeps_rules_for_failed_kuhb_and_cleans_successful_peer(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            defaults = make_defaults(tmp_path)
            defaults["batch_size"]["remove"] = 1
            logger = EventLogger(tmp_path / "kuhbs.log", color=False, stdout=False)
            events = []
            runner = SnitchRuleRunner(
                logger,
                rule_files={"app-alpha.yml", "app-beta.yml"},
                managed_vms={"tpl-alpha", "app-alpha", "tpl-beta", "app-beta"},
                fail_remove_vms={"app-alpha"},
                events=events,
            )
            ctx = SnitchRuleContext(defaults, logger, runner)
            definitions = tuple(make_remove_definition(defaults, kuhb_id) for kuhb_id in ("alpha", "beta"))

            with patch("builtins.input", return_value="y"):
                with self.assertRaises(RuntimeError):
                    remove.run_all(ctx, definitions)

            self.assertNotIn("rule-remove:app-alpha.yml", events)
            self.assertIn("rule-remove:app-beta.yml", events)
            self.assertEqual(events.count("snitch-restart"), 1)


if __name__ == "__main__":
    unittest.main()
