# Purpose: Hook and setup-script execution for dom0 and target kuhs
# Scope: Failure preserves copied VM-side files so debugging has evidence
from __future__ import annotations

from pathlib import Path
import secrets
import shutil
from shlex import join
import time

from .command import CommandResult
from .config import resolve_path
from .qubes import run_in_kuh
from .scripts import concatenate_script_fragments, write_concatenated_script
from .terminal import dom0_terminal_script_args, terminal_script_args


VALID_HOOKS = {
    # Keep hook names finite so typos fail before user scripts run in dom0 or a target kuh
    "tpl": {"create-pre", "create-post", "remove", "update", "backup"},
    "app": {"create-pre", "create-post", "remove", "update", "backup"},
    "sta": {"create-pre", "create-post", "remove", "update", "backup"},
    "ndp": {"create-pre", "create-post", "remove"},
    "sta-setup-template": {"create-pre", "create-post"},
}


def kuhb_dir(definition: dict, ctx) -> Path:
    # Active definitions always live under the configured My KUHBS root
    return ctx.kuhbs_root / definition["id"]


def hook_paths(root: str | Path, kuh_kind: str, instance_id: str | None, hook: str) -> list[Path]:
    # Instance hooks add behavior without replacing type-level hooks
    if hook not in VALID_HOOKS.get(kuh_kind, set()):
        raise ValueError(f"invalid hook {hook} for {kuh_kind}")
    base = Path(root) / "hooks" / kuh_kind
    paths = [base / f"{hook}.sh"]
    if instance_id:
        paths.append(base / instance_id / f"{hook}.sh")
    return [path for path in paths if path.exists()]


def run_hook_in_dom0(ctx, kuhb_id: str, kuh_name: str, script: str | Path) -> None:
    # Dom0 hooks are arbitrary user programs: they may prompt, use full-screen menus or read passwords
    script_path = Path(script)
    runner_path = _script_runner_path(ctx)
    exit_status_file = f"/tmp/.kuhbs-script-exit-status-{secrets.token_hex(8)}"
    # A shared parent terminal cannot route concurrent hook input, while serializing hooks would stall unrelated workers
    ctx.logger.status(kuhb_id, f"Executing hook {script_path}")
    ctx.runner.run(["rm", "-f", exit_status_file], source=kuh_name)
    title = f"KUHBS: dom0 {kuh_name} {script_path.stem}"
    terminal_args = dom0_terminal_script_args(
        ctx,
        str(runner_path),
        exit_status_file,
        str(script_path),
        title=title,
    )
    # The dedicated terminal supplies isolated stdin/stdout; the wrapper marker supplies the exit code xfce cannot
    _wait_for_terminal_script_status(
        ctx,
        kuh_name,
        script_path,
        terminal_args,
        exit_status_file,
        status_location="dom0",
    )
    # Failed hooks bypass cleanup so their status marker remains available with the original dom0 script
    ctx.runner.run(["rm", "-f", exit_status_file], source=kuh_name, check=False)


def _script_runner_path(ctx) -> Path:
    # Both local dom0 hooks and copied VM scripts depend on the configured installed KUHBS wrapper
    installed = resolve_path(ctx.defaults["paths"]["setup_scripts"]) / "kuhbs" / "kuhbs-run-script.sh"
    if not installed.exists():
        raise FileNotFoundError(f"KUHBS script runner missing: {installed}")
    return installed


def _run_terminal_status_command(ctx, kuh_name: str, args: list[str], *, status_location: str, log: bool) -> CommandResult:
    # The marker protocol is identical; only its command transport differs between local dom0 and qvm-run
    if status_location == "dom0":
        return ctx.runner.run(args, source=kuh_name, check=False, log=log)
    if status_location == "kuh":
        return run_in_kuh(ctx.runner, kuh_name, args, user="root", check=False, log=log)
    raise ValueError(f"unsupported terminal script status location: {status_location}")


def _read_terminal_script_status(
    ctx,
    kuh_name: str,
    exit_status_file: str,
    *,
    status_location: str,
) -> int | None:
    # The wrapper publishes its executable marker atomically, so existence means it is safe to run immediately
    exists = _run_terminal_status_command(
        ctx,
        kuh_name,
        ["test", "-f", exit_status_file],
        status_location=status_location,
        log=False,
    )
    if exists.returncode != 0:
        return None
    result = _run_terminal_status_command(
        ctx,
        kuh_name,
        [exit_status_file],
        status_location=status_location,
        log=True,
    )
    return result.returncode


def _wait_for_terminal_script_status(
    ctx,
    kuh_name: str,
    script_path: Path,
    terminal_args: list[str],
    exit_status_file: str,
    *,
    status_location: str = "kuh",
) -> None:
    # Terminal emulators report UI lifetime, not the wrapped script result, so both locations use the marker
    if status_location == "dom0":
        status_check_args = ["test", "-f", exit_status_file]
        status_identity = "dom0"
    elif status_location == "kuh":
        status_check_args = ["qvm-run", "--quiet", "--user", "root", kuh_name, "test", "-f", exit_status_file]
        status_identity = kuh_name
    else:
        raise ValueError(f"unsupported terminal script status location: {status_location}")
    process = ctx.runner.start(terminal_args, source=kuh_name)
    ctx.logger.info(kuh_name, f"Waiting for the script to complete by executing {join(status_check_args)} once a second, to determine if the script has completed yet")
    status_seen = False
    script_returncode = 0
    try:
        while True:
            if not status_seen:
                observed_returncode = _read_terminal_script_status(
                    ctx,
                    kuh_name,
                    exit_status_file,
                    status_location=status_location,
                )
                if observed_returncode is not None:
                    status_seen = True
                    script_returncode = observed_returncode
            process_status = ctx.runner.poll(process)
            if process_status is not None:
                terminal_result = ctx.runner.finish(process, terminal_args, source=kuh_name, check=False, log=False)
                if not status_seen:
                    # The terminal and wrapper can finish between the periodic marker check and this process poll
                    observed_returncode = _read_terminal_script_status(
                        ctx,
                        kuh_name,
                        exit_status_file,
                        status_location=status_location,
                    )
                    if observed_returncode is not None:
                        status_seen = True
                        script_returncode = observed_returncode
                if not status_seen:
                    raise RuntimeError(f"script terminal closed before writing exit status: {status_identity}:{script_path}")
                if script_returncode != 0:
                    # Wait for the optional debug shell to close before callers clean up its VM or dom0 evidence
                    raise RuntimeError(f"failed with exit status {script_returncode}: {status_identity}:{script_path}")
                if terminal_result.returncode != 0:
                    raise RuntimeError(f"script terminal failed with exit status {terminal_result.returncode}: {status_identity}:{script_path}")
                return
            time.sleep(1)
    except KeyboardInterrupt:
        # Direct operation Ctrl+C has no batch coordinator to kill and reap this tracked terminal process
        ctx.runner.kill_active_processes()
        ctx.runner.finish(process, terminal_args, source=kuh_name, check=False, log=False)
        raise


def run_script_in_kuh_terminal(ctx, kuhb_id: str, kuh_name: str, script: str | Path, *, user: str = "root", script_args: list[str] | None = None) -> None:
    # Copy and run one trusted script through the installed VM-side status wrapper
    script_path = Path(script)
    runner_path = _script_runner_path(ctx)
    remote_dir = "/home/user/QubesIncoming/dom0"
    remote_runner = f"{remote_dir}/{runner_path.name}"
    remote_script = f"{remote_dir}/{script_path.name}"
    exit_status_file = f"/tmp/.kuhbs-script-exit-status-{secrets.token_hex(8)}"
    # Failed runs may leave copied scripts behind for debugging; clear only the names this run will overwrite
    run_in_kuh(ctx.runner, kuh_name, ["rm", "-f", remote_runner, remote_script], user="root")
    # qvm-copy-to-vm stores dom0 copies under QubesIncoming/dom0 inside the target kuh
    ctx.runner.run(["qvm-copy-to-vm", kuh_name, str(runner_path), str(script_path)], source=kuh_name)
    run_in_kuh(ctx.runner, kuh_name, ["rm", "-f", exit_status_file], user="root")
    # Bash reads the target script file so it needs read permission as well as execute
    run_in_kuh(ctx.runner, kuh_name, ["chmod", "500", remote_runner, remote_script], user="root")
    title = f"KUHBS: {kuh_name} {script_path.stem}"
    terminal_args = ["qvm-run", "--quiet", "--user", user, kuh_name] + terminal_script_args(ctx, kuh_name, remote_runner, exit_status_file, remote_script, script_args=script_args, title=title)
    _wait_for_terminal_script_status(ctx, kuh_name, script_path, terminal_args, exit_status_file)
    # Preserve failed VM-side scripts/status files; successful runs clean individual copied files
    run_in_kuh(ctx.runner, kuh_name, ["rm", "-f", remote_runner, remote_script, exit_status_file], user="root", check=False)


def run_hook_in_kuh(ctx, kuhb_id: str, kuh_name: str, script: str | Path) -> None:
    # Run a hook in its target kuh through the normal terminal path
    run_script_in_kuh_terminal(ctx, kuhb_id, kuh_name, script)


def run_hook(ctx, kuhb_id: str, kuh_name: str, script: str | Path, *, location: str = "kuh") -> None:
    # Dispatch one validated hook to its declared authority boundary
    if location == "dom0":
        run_hook_in_dom0(ctx, kuhb_id, kuh_name, script)
    elif location == "kuh":
        run_hook_in_kuh(ctx, kuhb_id, kuh_name, script)
    else:
        raise ValueError(f"unsupported hook location: {location}")


def run_hooks(ctx, definition: dict, kuh, hook: str, *, location: str = "kuh") -> None:
    # Run matching type and instance hooks in stable path order
    for script in hook_paths(kuhb_dir(definition, ctx), kuh.kind, kuh.instance_id, hook):
        run_hook(ctx, definition["id"], kuh.name, script, location=location)


def setup_script_paths(root: str | Path, kuh) -> list[Path]:
    # setup_scripts entries are explicit dom0 paths.  KUHBS-provided fragments live
    # under /usr/share/kuhbs/setup-scripts; custom user fragments can live under
    # ~/.kuhbs/setup-scripts or any other absolute path the user chooses.
    # Local scripts/<kind> are appended as product defaults.
    # setup_scripts are already merged onto the kuh config that will run them.
    # This keeps app/tpl/sta setup intent next to the VM config it modifies.
    # One pure path expansion is shared by startup validation and runtime setup
    root = Path(root)
    paths: list[Path] = []
    for script_path in kuh.config.get("setup_scripts", []):
        path = resolve_path(script_path)
        if not path.is_absolute():
            raise ValueError(f"setup_scripts entries must be absolute paths: {script_path}")
        paths.append(path)
    local = root / "scripts" / kuh.kind
    if local.exists():
        paths.append(local)
    return paths


def setup_template_dir(root: str | Path, kuh_kind: str) -> Path | None:
    # KUHB setup payloads have one explicit location copied before setup scripts run
    root = Path(root)
    path = root / "templates" / kuh_kind
    if not path.exists():
        return None
    if not path.is_dir():
        raise ValueError(f"kuhb setup template root must be a directory: {path}")
    return path


def _merge_template_dir(source: Path, target: Path) -> None:
    # copytree keeps dotfiles/symlinks and intentionally lets later template layers overwrite earlier files
    shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)


def _copy_setup_templates(ctx, definition: dict, kuh) -> None:
    # Merge and copy common plus KUHB-specific setup payloads
    root = kuhb_dir(definition, ctx)
    remote_dir = "/home/user/QubesIncoming/dom0"
    setup_root = resolve_path(ctx.defaults["paths"]["setup_scripts"])
    staging_root = resolve_path(ctx.defaults["paths"]["scripts"]).parent / "template-copies" / kuh.name
    staging = staging_root / "templates"
    local_templates = setup_template_dir(root, kuh.kind)
    common_templates = setup_root / "kuhbs" / "setup" / "templates"
    if not local_templates and not common_templates.exists():
        ctx.logger.info(definition["id"], f"Not copying absent setup templates for {kuh.name}")
        return
    ctx.logger.status(definition["id"], f"Copying setup templates to {kuh.name}:templates")
    # qvm-copy-to-vm preserves the basename, so stage the merged tree as plain 'templates'
    shutil.rmtree(staging_root, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    if common_templates.exists():
        _merge_template_dir(common_templates, staging)
    if local_templates:
        # Kuhb-specific templates are copied last so they can deliberately override shared files
        _merge_template_dir(local_templates, staging)
    try:
        run_in_kuh(ctx.runner, kuh.name, ["rm", "-rf", f"{remote_dir}/templates"], user="root")
        ctx.runner.run(["qvm-copy-to-vm", kuh.name, str(staging)], source=kuh.name)
    finally:
        # Staged template payloads can contain secrets, so dom0 cleanup runs even when copy fails
        shutil.rmtree(staging_root, ignore_errors=True)


def run_setup_scripts(ctx, definition: dict, kuh) -> None:
    # Setup only runs when there are trusted fragments; hooks cover lightweight per-stage customization
    paths = setup_script_paths(kuhb_dir(definition, ctx), kuh)
    if not paths:
        return
    concatenated = concatenate_script_fragments(paths)
    output = resolve_path(ctx.defaults["paths"]["scripts"]) / definition["id"] / f"setup-{kuh.name}.sh"
    write_concatenated_script(output, concatenated)
    _copy_setup_templates(ctx, definition, kuh)
    ctx.logger.status(definition["id"], f"Running setup scripts for {kuh.name}")
    run_script_in_kuh_terminal(ctx, definition["id"], kuh.name, output)
    # Create-time setup script directories own the whole incoming tree for this operation
    run_in_kuh(ctx.runner, kuh.name, ["rm", "-rf", "/home/user/QubesIncoming/"], user="root")
