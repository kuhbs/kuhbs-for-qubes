# Purpose: Regression tests for ordered upgrade batching
# Scope: No real Qubes commands; fake statuses and workers only
from __future__ import annotations

from contextlib import redirect_stdout
from pathlib import Path
import io
import tempfile
import threading
import unittest
from unittest.mock import patch

from kuhbs.command import CommandResult, CommandRunner
from helpers import RecordingEventLogger as EventLogger
from kuhbs.operations import OperationContext
from kuhbs.operations import upgrade_batch
from helpers import current_defaults


def definition(kuhb_id: str, order: int, confirm_network: bool = False) -> dict:
    # Minimal merged definition shape needed by the batch planner
    return {
        "id": kuhb_id,
        "name": kuhb_id,
        "order": order,
        "confirm_network_after_upgrade": confirm_network,
    }


class BatchRunner(CommandRunner):
    # Capture dom0 commands so tests can verify dom0 is last without running real Qubes tools
    def __init__(self, logger):
        super().__init__(logger)
        self.commands: list[list[str]] = []

    def run(self, args, *, source="dom0", check=True, visible_output=False, log=True):
        self.commands.append(list(args))
        returncode = 1 if args[:3] == ["qvm-check", "--quiet", "--paused"] else 0
        return CommandResult(returncode=returncode)


class BatchContext(OperationContext):
    def __init__(self, root: Path):
        self.batch_runner = BatchRunner(EventLogger(root / "kuhbs.log", stdout=False))
        super().__init__(current_defaults({"paths": {"config": str(root), "kuhbs": str(root / "my-kuhbs")}, "upgrade": {"restart_without_prompt": []}}), self.batch_runner.logger)

    @property
    def runner(self):
        return self.batch_runner


class UpgradeBatchTests(unittest.TestCase):
    def test_qubes_template_upgrade_runs_the_exact_four_steps(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = BatchContext(Path(td))

            with patch("kuhbs.operations.upgrade.run_script_in_kuh_terminal") as run_script:
                upgrade_batch.upgrade.upgrade_qubes_template_vm(ctx, "debian-13-minimal")

            self.assertEqual(
                ctx.batch_runner.commands,
                [
                    ["qvm-shutdown", "--wait", "--force", "debian-13-minimal"],
                    ["qvm-template", "upgrade", "debian-13-minimal"],
                    ["qvm-shutdown", "--wait", "--force", "debian-13-minimal"],
                ],
            )
            apt_update_script = Path(ctx.defaults["paths"]["setup_scripts"]) / "kuhbs/setup/apt-update.sh"
            run_script.assert_called_once_with(ctx, "debian-13-minimal", "debian-13-minimal", apt_update_script)

    def test_qubes_template_upgrade_forces_the_final_shutdown_after_apt_failure(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = BatchContext(Path(td))

            with patch(
                "kuhbs.operations.upgrade.run_script_in_kuh_terminal",
                side_effect=RuntimeError("apt failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "apt failed"):
                    upgrade_batch.upgrade.upgrade_qubes_template_vm(ctx, "debian-13-minimal")

            self.assertEqual(
                ctx.batch_runner.commands,
                [
                    ["qvm-shutdown", "--wait", "--force", "debian-13-minimal"],
                    ["qvm-template", "upgrade", "debian-13-minimal"],
                    ["qvm-shutdown", "--wait", "--force", "debian-13-minimal"],
                ],
            )

    def test_qubes_templates_run_in_parallel_after_kuhbs_and_dom0(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = BatchContext(Path(td))
            events: list[str] = []
            barrier = threading.Barrier(2)

            def fake_run(_ctx, item):
                events.append(item["id"])
                return upgrade_batch.upgrade.RestartPlan()

            def fake_dom0(*_args, **_kwargs):
                events.append("dom0")
                return CommandResult(returncode=0)

            def fake_template(_ctx, vm_name):
                barrier.wait(timeout=2)
                events.append(vm_name)

            with patch("kuhbs.operations.upgrade_batch.upgrade.run", side_effect=fake_run), \
                 patch.object(ctx.runner, "run_for_dom0", side_effect=fake_dom0), \
                 patch("kuhbs.operations.upgrade_batch.upgrade.upgrade_qubes_template_vm", side_effect=fake_template):
                upgrade_batch.run_targets(
                    ctx,
                    [definition("signal", 500)],
                    upgrade_dom0=True,
                    qubes_templates=("debian-13-minimal", "whonix-workstation-18"),
                )

            self.assertEqual(events[:2], ["signal", "dom0"])
            self.assertEqual(set(events[2:]), {"debian-13-minimal", "whonix-workstation-18"})

    def test_batches_by_order_and_dom0_last(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = BatchContext(Path(td))
            targets = [
                definition("c", 600),
                definition("a", 500),
                definition("b", 500),
            ]
            calls: list[str] = []

            def fake_run(_ctx, item):
                calls.append(item["id"])
                return upgrade_batch.upgrade.RestartPlan()

            with patch("kuhbs.operations.upgrade_batch.upgrade.run", side_effect=fake_run):
                upgrade_batch.run_targets(ctx, targets, upgrade_dom0=True)

            self.assertEqual(set(calls[:2]), {"a", "b"})
            self.assertEqual(calls[2:], ["c"])
            self.assertEqual(ctx.batch_runner.commands, [["sudo", "qubes-dom0-update"]])

    def test_dom0_upgrade_writes_completed_health_state(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = BatchContext(Path(td))

            upgrade_batch.run_targets(ctx, [], upgrade_dom0=True)

            self.assertEqual(ctx.state_store.get_state("dom0", "upgrade"), "completed")

    def test_dom0_upgrade_failure_writes_failed_health_state(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = BatchContext(Path(td))

            with patch.object(ctx.runner, "run_for_dom0", side_effect=RuntimeError("boom")):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    upgrade_batch.run_targets(ctx, [], upgrade_dom0=True)

            self.assertEqual(ctx.state_store.get_state("dom0", "upgrade"), "failed")

    def test_network_confirmation_after_marked_group(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = BatchContext(Path(td))
            targets = [definition("net", 2, confirm_network=True)]

            with patch("kuhbs.operations.upgrade_batch.upgrade.run", return_value=upgrade_batch.upgrade.RestartPlan()), patch("builtins.input", return_value="") as prompt:
                upgrade_batch.run_targets(ctx, targets)

            prompt.assert_called_once()

    def test_same_order_failure_lets_siblings_finish_then_aborts(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = BatchContext(Path(td))
            targets = [
                definition("a", 500),
                definition("b", 500),
                definition("c", 600),
            ]
            calls: list[str] = []

            def fake_run(_ctx, item):
                calls.append(item["id"])
                if item["id"] == "a":
                    raise RuntimeError("boom")
                return upgrade_batch.upgrade.RestartPlan()

            with patch("kuhbs.operations.upgrade_batch.upgrade.run", side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "upgrade failed for:\na: boom"):
                    upgrade_batch.run_targets(ctx, targets, upgrade_dom0=True)

            self.assertIn("b", calls)
            self.assertNotIn("c", calls)
            self.assertEqual(ctx.batch_runner.commands, [])
            log_text = (Path(td) / "kuhbs.log").read_text()
            self.assertIn("ERROR     kuhbs                     Upgrade order failed:", log_text)
            self.assertIn("ERROR     kuhbs                     a: boom", log_text)

    def test_same_order_failures_are_reported_on_separate_lines(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = BatchContext(Path(td))
            targets = [
                definition("hermes", 500),
                definition("vm-2", 500),
            ]

            def fake_run(_ctx, item):
                raise RuntimeError("command failed with exit 100: qvm-run --quiet --user root tpl-hermes apt-get update")

            with patch("kuhbs.operations.upgrade_batch.upgrade.run", side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "upgrade failed for:\n"):
                    upgrade_batch.run_targets(ctx, targets)

            log_text = (Path(td) / "kuhbs.log").read_text()
            self.assertIn("ERROR     kuhbs                     Upgrade order failed:\n", log_text)
            self.assertIn("ERROR     kuhbs                     hermes: command failed with exit 100: qvm-run --quiet --user root tpl-hermes apt-get update\n", log_text)
            self.assertIn("ERROR     kuhbs                     vm-2: command failed with exit 100: qvm-run --quiet --user root tpl-hermes apt-get update", log_text)

    def test_same_order_restart_prompt_is_aggregated_once(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = BatchContext(Path(td))
            targets = [
                definition("a", 500),
                definition("b", 500),
            ]

            def fake_run(_ctx, item):
                plan = upgrade_batch.upgrade.RestartPlan()
                plan.restart.append(f"app-{item['id']}")
                return plan

            stdout = io.StringIO()
            with patch("kuhbs.operations.upgrade_batch.upgrade.run", side_effect=fake_run):
                with patch("builtins.input", return_value="") as prompt:
                    with redirect_stdout(stdout):
                        upgrade_batch.run_targets(ctx, targets)

            prompt.assert_called_once_with("Would you like to restart them now? [Y/n] ")
            prompt_text = stdout.getvalue()
            self.assertIn("app-a, app-b", prompt_text)
            self.assertIn(["qvm-shutdown", "--wait", "--force", "app-a"], ctx.batch_runner.commands)
            self.assertIn(["qvm-start", "app-a"], ctx.batch_runner.commands)
            self.assertIn(["qvm-shutdown", "--wait", "--force", "app-b"], ctx.batch_runner.commands)
            self.assertIn(["qvm-start", "app-b"], ctx.batch_runner.commands)

    def test_listed_restarts_are_applied_without_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = BatchContext(Path(td))
            ctx.defaults["upgrade"]["restart_without_prompt"] = ["ndp-kuhbs-net-cache"]
            targets = [definition("gateway", 3)]

            def fake_run(_ctx, item):
                plan = upgrade_batch.upgrade.RestartPlan()
                plan.restart.append("ndp-kuhbs-net-cache")
                return plan

            with patch("kuhbs.operations.upgrade_batch.upgrade.run", side_effect=fake_run):
                with patch("builtins.input") as prompt:
                    upgrade_batch.run_targets(ctx, targets)

            prompt.assert_not_called()
            self.assertIn(["qvm-shutdown", "--wait", "--force", "ndp-kuhbs-net-cache"], ctx.batch_runner.commands)
            self.assertIn(["qvm-start", "ndp-kuhbs-net-cache"], ctx.batch_runner.commands)

    def test_declining_group_restart_still_runs_dom0_without_extra_warning(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = BatchContext(Path(td))
            targets = [definition("a", 500)]

            def fake_run(_ctx, item):
                plan = upgrade_batch.upgrade.RestartPlan()
                plan.restart.append("app-a")
                return plan

            with patch("kuhbs.operations.upgrade_batch.upgrade.run", side_effect=fake_run):
                with patch("kuhbs.operations.upgrade_batch.upgrade._confirm_restart", return_value=False):
                    upgrade_batch.run_targets(ctx, targets, upgrade_dom0=True)

            log_text = (Path(td) / "kuhbs.log").read_text()
            self.assertEqual(ctx.batch_runner.commands, [["sudo", "qubes-dom0-update"]])
            self.assertNotIn("Upgrade completed, but running VMs may still use old template/app state", log_text)

    def test_restart_failure_is_reported_as_order_failure(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = BatchContext(Path(td))
            targets = [definition("a", 500)]

            def fake_run(_ctx, item):
                plan = upgrade_batch.upgrade.RestartPlan()
                plan.restart.append("bad-vm")
                return plan

            def fail_restart(_ctx, _kuhb_id, _plan):
                raise RuntimeError("command failed with exit 100: qvm-shutdown --wait --force bad-vm")

            with patch("kuhbs.operations.upgrade_batch.upgrade.run", side_effect=fake_run):
                with patch("kuhbs.operations.upgrade_batch.upgrade._confirm_restart", return_value=True):
                    with patch("kuhbs.operations.upgrade_batch.upgrade._apply_restart_plan", side_effect=fail_restart):
                        with self.assertRaisesRegex(RuntimeError, "restart: command failed with exit 100"):
                            upgrade_batch.run_targets(ctx, targets, upgrade_dom0=True)

            self.assertEqual(ctx.batch_runner.commands, [])

    def test_network_failure_stops_immediately(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = BatchContext(Path(td))
            targets = [
                definition("net-nic", 2, confirm_network=True),
                definition("later", 500),
            ]

            with patch("kuhbs.operations.upgrade_batch.upgrade.run", side_effect=RuntimeError("wifi down")):
                with self.assertRaisesRegex(RuntimeError, "upgrade failed for:\nnet-nic: wifi down"):
                    upgrade_batch.run_targets(ctx, targets)


if __name__ == "__main__":
    unittest.main()
