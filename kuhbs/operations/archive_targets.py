# Purpose: Shared backup/restore target resolution and parallel execution
# Scope: Keep explicit and all backup/restore commands on one target model
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable
from shlex import quote

from ..model import Kuh
from ..qubes import vm_exists
from . import OperationContext


@dataclass(frozen=True)
class ArchiveTarget:
    # id names one archive while kuhb_id owns the shared lifecycle state
    id: str
    kuhb_id: str
    definition: dict
    kuh: Kuh


@dataclass(frozen=True)
class Dom0ArchiveTarget:
    # Dom0 is explicit archive work, not a nullable fake VM definition
    id: str = "dom0"
    kuhb_id: str = "dom0"


ArchiveRequest = ArchiveTarget | Dom0ArchiveTarget


@dataclass(frozen=True)
class ArchiveJob:
    # One worker owns one operation state file for one KUHB or one dom0 target
    kuhb_id: str
    targets: tuple[ArchiveTarget, ...]


def _enabled_targets_for_definition(ctx: OperationContext, definition: dict, enabled_kuhs: Callable[[OperationContext, dict], list[Kuh]]) -> list[ArchiveTarget]:
    # Keep each kuhb's resolve_kuhs order so related tpl/app/sta archives stay grouped
    ctx.register_kuh_labels(definition)
    enabled = [
        ArchiveTarget(kuh.name, definition["id"], definition, kuh)
        for kuh in enabled_kuhs(ctx, definition)
    ]
    # A KUHB with no configured archive paths is intentionally not an archive target
    return enabled


def targets_for_all(ctx: OperationContext, definitions: tuple[dict, ...], enabled_kuhs: Callable[[OperationContext, dict], list[Kuh]], *, include_dom0: bool) -> list[ArchiveRequest]:
    # Planner-selected KUHBs are sorted by id while their resolved KUH order stays intact
    targets: list[ArchiveRequest] = []
    for definition in sorted(definitions, key=lambda item: item["id"]):
        targets.extend(_enabled_targets_for_definition(ctx, definition, enabled_kuhs))
    if include_dom0:
        # dom0 stays in the approved plan; backup/restore reports disabled config during execution
        targets.append(Dom0ArchiveTarget())
    return targets


def validate_existing_kuhs(ctx: OperationContext, kuhs: list[Kuh]) -> None:
    # Backup and restore execute against real VMs after state planning has already allowed the KUHB
    # Keep this as execution failure, not plan truth, so failed runs still write operation state
    for kuh in kuhs:
        if not vm_exists(ctx.runner, kuh.name):
            raise RuntimeError(f"Required kuh missing: {kuh.name}")


@contextmanager
def temporary_qrexec_policy(ctx: OperationContext, policy_path: str, policy_line: str):
    # Backup/restore qrexec services are opened only for the active archive stream
    # Cleanup starts before install so one normal Ctrl-C cannot leave a created policy behind
    try:
        ctx.runner.shell_for_dom0(f"echo {quote(policy_line)} | sudo install -m 0644 /dev/stdin {quote(policy_path)}")
        yield
    finally:
        # rm -f succeeds whether the temporary policy exists or is already absent
        ctx.runner.run_for_dom0(["sudo", "rm", "-f", policy_path])


def _job_list(targets: list[ArchiveTarget]) -> list[ArchiveJob]:
    # Preserve target order while grouping VM targets so summaries match the requested plan
    jobs_by_kuhb: dict[str, list[ArchiveTarget]] = {}
    ordered_ids: list[str] = []
    for target in targets:
        if target.kuhb_id not in jobs_by_kuhb:
            ordered_ids.append(target.kuhb_id)
            jobs_by_kuhb[target.kuhb_id] = []
        jobs_by_kuhb[target.kuhb_id].append(target)
    return [ArchiveJob(kuhb_id, tuple(jobs_by_kuhb[kuhb_id])) for kuhb_id in ordered_ids]


def _run_state_job(
    ctx: OperationContext,
    job: ArchiveJob,
    run_one: Callable[[ArchiveTarget], None],
    *,
    action: str,
) -> list[str]:
    # The worker owns start/completed/failed because -all and the executor are only wrappers
    ctx.set_state(job.kuhb_id, action, "start")
    try:
        completed: list[str] = []
        for target in job.targets:
            run_one(target)
            completed.append(target.id)
    except BaseException:
        # The parent batch kills shared child processes on Ctrl+C; ordinary worker failures stay isolated
        ctx.set_state(job.kuhb_id, action, "failed")
        raise
    # Final state output is outside the failure scope so an interrupted print cannot rewrite success as failed
    ctx.set_state(job.kuhb_id, action, "completed")
    return completed


def run_targets(
    ctx: OperationContext,
    targets: list[ArchiveTarget],
    run_one: Callable[[ArchiveTarget], None],
    *,
    source: str,
    action: str,
    success_phrase: str,
    failure_phrase: str,
) -> None:
    # Parallelism is limited by the operation's batch_size entry, not by -all command code
    max_workers = ctx.defaults["batch_size"][action]
    successes: list[str] = []
    failed_ids: list[str] = []
    jobs = _job_list(targets)
    if not jobs:
        ctx.logger.warning(source, "No archive targets selected")
        return

    with ThreadPoolExecutor(max_workers=min(max_workers, len(jobs))) as executor:
        futures = {
            executor.submit(_run_state_job, ctx, job, run_one, action=action): job
            for job in jobs
        }
        try:
            for future in as_completed(futures):
                job = futures[future]
                try:
                    successes.extend(future.result())
                except Exception as exc:
                    ctx.logger.error(job.kuhb_id, f"{action} failed; continuing: {exc}")
                    failed_ids.append(job.kuhb_id)
        except KeyboardInterrupt:
            # Cancel queued work, kill active child groups and let workers write their failed state
            for future in futures:
                future.cancel()
            ctx.runner.kill_active_processes()
            raise RuntimeError(f"{source} interrupted; killed active KUHBS commands")

    success_summary = ", ".join(target.id for target in targets if target.id in successes) if successes else "none"
    ctx.logger.status(source, f"Successfully {success_phrase} kuhs: {success_summary}")
    if failed_ids:
        failure_summary = ", ".join(job.kuhb_id for job in jobs if job.kuhb_id in failed_ids)
        ctx.logger.error(source, f"Failed to {failure_phrase} kuhs: {failure_summary}")
        raise RuntimeError(f"{source} failed")
