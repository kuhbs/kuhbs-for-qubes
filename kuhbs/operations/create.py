# Purpose: Create operation for templates, apps, standalones, and named disposables
# Scope: Validation runs before qvm mutations so failures are early
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from ..hooks import kuhb_dir, run_hooks, run_setup_scripts
from ..launchers import generate_launchers
from ..model import Kuh, resolve_kuh_name, resolve_kuhs
from ..qubes import shutdown_if_running, vm_exists
from . import OperationContext
from .backup import backup_paths_for_kuh
from .restore import restore_kuh_archive_if_present


# Probe likely dependents first so a collision names the VM the operator usually recognizes
CREATE_EXISTENCE_CHECK_ORDER = ("app", "ndp", "tpl", "sta")


def _creation_targets(definition: dict, kuhs: list[Kuh]) -> list[str]:
    # Standalone creation also mutates one temporary template that must not pre-exist
    targets: list[str] = []
    for kind in CREATE_EXISTENCE_CHECK_ORDER:
        targets.extend(kuh.name for kuh in kuhs if kuh.kind == kind)
    if any(kuh.kind == "sta" for kuh in kuhs):
        targets.insert(0, f"tpl-{definition['id']}-setup-tmp")
    return targets


def _abort_if_any_creation_target_exists(ctx: OperationContext, definition: dict, kuhs: list[Kuh]) -> None:
    # Create is not repair: preflight every target before the first qvm-create/qvm-clone can mix old and new VMs
    existing = [name for name in _creation_targets(definition, kuhs) if vm_exists(ctx.runner, name)]
    if existing:
        raise RuntimeError(f"refusing to create {definition['id']}; VM already exists: {', '.join(existing)}")


def _label(ctx: OperationContext, kuh: Kuh) -> str:
    # Startup validation guarantees non-template labels and defaults.tpl.label exist.
    return str(kuh.config["prefs"]["label"])


def _create_kuh(ctx: OperationContext, definition: dict, kuh: Kuh) -> None:
    # Create the Qubes resource first, then apply prefs/services/features uniformly
    runner = ctx.runner
    ctx.logger.status(definition["id"], f"Creating {kuh.name}")
    if kuh.kind == "tpl":
        # The base template is a top-level kuhb decision, not a hidden default or per-kuh value
        template = definition["template"]
        runner.run_for_kuh(kuh.name, ["qvm-clone", template, kuh.name])
    elif kuh.kind == "app":
        # AppVMs in a kuhb always use the kuhb's one TemplateVM; YAML does not repeat that obvious link
        template = resolve_kuh_name(definition["id"], "tpl")
        runner.run_for_kuh(kuh.name, ["qvm-create", "--class", "AppVM"] + ["--label", _label(ctx, kuh), "--template", template, kuh.name])
    elif kuh.kind == "ndp":
        # Named disposable templates are AppVM-derived, so they always use the kuhb's app template
        template = resolve_kuh_name(definition["id"], "app")
        runner.run_for_kuh(kuh.name, ["qvm-create", "--class", "DispVM"] + ["--label", _label(ctx, kuh), "--template", template, kuh.name])
    _apply_metadata(ctx, definition, kuh)
    _disable_qvm_revisions(ctx, kuh)
    _set_volatile_ephemeral(ctx, kuh)


def _apply_metadata(ctx: OperationContext, definition: dict, kuh: Kuh) -> None:
    # Apply every validated pref, service and feature after VM creation
    merged = kuh.config
    for key, value in merged["prefs"].items():
        # qvm-create needs --label, while qvm-clone needs the cloned label corrected afterward
        runner_value = "" if value is None else str(value)
        ctx.runner.run_for_kuh(kuh.name, ["qvm-prefs", kuh.name, key, runner_value])
    for service, enabled in merged["services"].items():
        ctx.runner.run_for_kuh(kuh.name, ["qvm-service", kuh.name, service, "on" if enabled else "off"])
    for feature, value in merged["features"].items():
        ctx.runner.run_for_kuh(kuh.name, ["qvm-features", kuh.name, feature, str(value)])


def _disable_qvm_revisions(ctx: OperationContext, kuh: Kuh) -> None:
    # KUHBS has its own backup path; Qubes root/private revisions waste space for managed kuhs
    if kuh.kind in {"tpl", "sta"}:
        ctx.runner.run_for_kuh(kuh.name, ["qvm-volume", "config", f"{kuh.name}:root", "revisions_to_keep", "0"])
        ctx.runner.run_for_kuh(kuh.name, ["qvm-volume", "config", f"{kuh.name}:private", "revisions_to_keep", "0"])
    elif kuh.kind == "app":
        ctx.runner.run_for_kuh(kuh.name, ["qvm-volume", "config", f"{kuh.name}:private", "revisions_to_keep", "0"])


def _set_volatile_ephemeral(ctx: OperationContext, kuh: Kuh) -> None:
    # Qubes requires the VM to be powered off before changing volatile volume encryption
    merged = kuh.config
    ctx.runner.run_for_kuh(kuh.name, ["qvm-volume", "config", f"{kuh.name}:volatile", "ephemeral", str(merged["volume_ephemeral"])])


def _seed_snitch_rules_if_present(ctx: OperationContext, definition: dict, kuh: Kuh) -> None:
    # Starter rules live next to the KUHB so initial firewall policy is reviewed with the VM definition
    source = kuhb_dir(definition, ctx) / "snitch-rules" / f"{kuh.name}.yml"
    if not source.exists():
        return
    qubes_snitch = ctx.defaults["firewall"]["qubes_snitch"]
    snitch_vm = qubes_snitch["snitch_vm"]
    target = f"{qubes_snitch['rules_dir']}/{source.name}"
    incoming = f"/home/user/QubesIncoming/dom0/{source.name}"
    if not vm_exists(ctx.runner, snitch_vm):
        ctx.logger.warning(definition["id"], f"Skipping Qubes Snitch starter rules because {snitch_vm} does not exist")
        return
    if ctx.runner.run_for_kuh(snitch_vm, ["qvm-run", snitch_vm, "test", "-f", target], check=False).returncode == 0:
        ctx.logger.warning(definition["id"], f"Keeping existing Qubes Snitch rules for {kuh.name}: {target}")
        return
    ctx.logger.status(definition["id"], f"Copying starter Qubes Snitch rules from {source} to {snitch_vm}:{target}")
    ctx.runner.run_for_kuh(snitch_vm, ["qvm-copy-to-vm", snitch_vm, str(source)])
    # Snitch generates root:root mode 0644 rule files, so starter rules use the same final metadata
    ctx.runner.run_for_kuh(snitch_vm, ["qvm-run", "--user", "root", snitch_vm, "install", "-o", "root", "-g", "root", "-m", "0644", "-v", incoming, target])
    # QubesIncoming is only staging and should not survive a successful persistent install
    ctx.runner.run_for_kuh(snitch_vm, ["qvm-run", "--user", "root", snitch_vm, "rm", "-f", incoming])
    ctx.runner.run_for_kuh(snitch_vm, ["qvm-run", "--user", "root", snitch_vm, "systemctl", "restart", "qubes-snitchd.service"])


def _refresh_appmenus(ctx: OperationContext, kuh: Kuh) -> None:
    # Qubes appmenus are generated after the kuh is quiet so launchers reflect installed apps
    shutdown_if_running(ctx.runner, kuh.name)
    ctx.runner.run_for_kuh(kuh.name, ["qvm-appmenus", "--update", "--force", kuh.name])


def _restore_kuh_if_present(ctx: OperationContext, definition: dict, kuh: Kuh) -> None:
    # Create restores user data before setup scripts so setup can migrate existing state
    if kuh.kind not in {"tpl", "app", "sta"} or backup_paths_for_kuh(kuh) is None:
        return
    restore_kuh_archive_if_present(ctx, definition, kuh)

def _create_standalone_kuhs(ctx: OperationContext, definition: dict, sta_kuhs: list[Kuh]) -> None:
    # Standalone VMs share one temporary setup template so expensive setup runs once
    if not sta_kuhs:
        return
    raw_sta = definition["kuhs"]["sta"]
    template = definition["template"]
    tmp_name = f"tpl-{definition['id']}-setup-tmp"
    setup_config = raw_sta["setup_template"]
    setup_kuh = Kuh(definition["id"], "sta-setup-template", tmp_name, None, setup_config)
    ctx.logger.set_source_label(tmp_name, _label(ctx, setup_kuh))
    ctx.logger.status(definition["id"], f"Creating standalone setup template {tmp_name}")
    ctx.runner.run_for_kuh(tmp_name, ["qvm-clone", template, tmp_name])
    _apply_metadata(ctx, definition, setup_kuh)
    _set_volatile_ephemeral(ctx, setup_kuh)
    run_hooks(ctx, definition, setup_kuh, "create-pre", location="dom0")
    run_setup_scripts(ctx, definition, setup_kuh)
    run_hooks(ctx, definition, setup_kuh, "create-post", location="dom0")
    # Qubes only makes template filesystem changes cloneable after the setup template is shut down
    shutdown_if_running(ctx.runner, tmp_name)
    for kuh in sta_kuhs:
        ctx.logger.status(definition["id"], f"Creating {kuh.name}")
        # qvm-clone preserves the source VM class; create a real StandaloneVM from the prepared setup template instead.
        ctx.runner.run_for_kuh(
            kuh.name,
            ["qvm-create", "--class", "StandaloneVM"]
            + ["--label", _label(ctx, kuh), "--template", tmp_name, kuh.name],
        )
        _apply_metadata(ctx, definition, kuh)
        _disable_qvm_revisions(ctx, kuh)
        _set_volatile_ephemeral(ctx, kuh)
        run_hooks(ctx, definition, kuh, "create-pre", location="dom0")
        _restore_kuh_if_present(ctx, definition, kuh)
        run_setup_scripts(ctx, definition, kuh)
        _refresh_appmenus(ctx, kuh)
        run_hooks(ctx, definition, kuh, "create-post", location="dom0")
        _seed_snitch_rules_if_present(ctx, definition, kuh)
    # Keep the setup template until every standalone clone succeeded so failures leave it available for debugging
    ctx.runner.run_for_kuh(tmp_name, ["qvm-remove", "--force", tmp_name])


def run(ctx: OperationContext, definition: dict) -> None:
    # Planning already admitted this KUHB; the worker owns the visible operation state
    ctx.register_kuh_labels(definition)
    kuhb_id = definition["id"]
    ctx.set_state(kuhb_id, "create", "start")
    try:
        kuhs = resolve_kuhs(definition)
        _abort_if_any_creation_target_exists(ctx, definition, kuhs)
        ctx.logger.status(kuhb_id, f"Starting create {kuhb_id}")
        _create_standalone_kuhs(ctx, definition, [kuh for kuh in kuhs if kuh.kind == "sta"])
        for kuh in kuhs:
            if kuh.kind == "sta":
                continue
            _create_kuh(ctx, definition, kuh)
            run_hooks(ctx, definition, kuh, "create-pre", location="dom0")
            _restore_kuh_if_present(ctx, definition, kuh)
            if kuh.kind != "ndp":
                run_setup_scripts(ctx, definition, kuh)
            _refresh_appmenus(ctx, kuh)
            run_hooks(ctx, definition, kuh, "create-post", location="dom0")
            _seed_snitch_rules_if_present(ctx, definition, kuh)
        generate_launchers(ctx, definition)
        # Appmenus refresh shuts VMs down, so honor Qubes autostart only after setup is fully done
        for kuh in kuhs:
            if kuh.kind != "tpl" and kuh.config["prefs"]["autostart"] is True:
                ctx.runner.run_for_kuh(kuh.name, ["qvm-start", kuh.name])
    except BaseException:
        # The parent batch kills shared child processes on Ctrl+C; ordinary worker failures stay isolated
        # Failed create resources remain for explicit `kuhbs remove` cleanup
        ctx.set_state(kuhb_id, "create", "failed")
        raise
    # Final state output is outside the failure scope so an interrupted print cannot rewrite success as failed
    ctx.set_state(kuhb_id, "create", "completed")


def run_all(ctx: OperationContext, definitions: tuple[dict, ...]) -> None:
    # KUHB order is the only dependency model: same-order KUHBs run in parallel, later orders wait
    by_order: dict[int, list[dict]] = {}
    for definition in definitions:
        by_order.setdefault(definition["order"], []).append(definition)
    batch_size = ctx.defaults["batch_size"]["create"]
    for order in sorted(by_order):
        group = sorted(by_order[order], key=lambda definition: definition["id"])
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=min(batch_size, len(group))) as executor:
            futures = {executor.submit(run, ctx, definition): definition for definition in group}
            try:
                for future in as_completed(futures):
                    definition = futures[future]
                    try:
                        future.result()
                    except Exception as exc:
                        ctx.logger.error(definition["id"], f"create failed: {exc}")
                        failures.append(definition["id"])
            except KeyboardInterrupt:
                # Cancel queued work, kill active child groups and let the executor wait for worker state cleanup
                for future in futures:
                    future.cancel()
                ctx.runner.kill_active_processes()
                raise RuntimeError("create interrupted; killed active KUHBS commands")
        if failures:
            # Later orders depend on earlier order setup being complete, so stop after the failed group
            raise RuntimeError(f"create failed at order {order}: {', '.join(sorted(failures))}")
