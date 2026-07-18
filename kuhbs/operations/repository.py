# Purpose: Manage trusted KUHB git repositories and explicit local links
# Scope: Repositories live under ~/.kuhbs/repos and active definitions stay in ~/.kuhbs/my-kuhbs
from __future__ import annotations

from pathlib import Path
import os
import re
import secrets
from shlex import quote
import shutil
import tempfile

from ..hooks import run_script_in_kuh_terminal
from ..repos import local_kuhb_paths
from ..validation import ConfigValidationError, validate_definition_changes, validate_definition_set, validate_kuhb_file
from . import OperationContext


# Hostnames keep dots while owner/project path components map to plain visible directories
REPO_RE = re.compile(r"[A-Za-z0-9.-]+/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*")
LINK_RE = re.compile(r"(?P<repo>[A-Za-z0-9.-]+/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*)/(?P<kuhb>[A-Za-z0-9_-]+)")
# The installed Bash script runs every Git command and user decision in the temporary repository VM
REPO_OPERATION_SCRIPT = Path("/usr/share/kuhbs/kuhbs-repo-operation.sh")
REMOTE_INCOMING = "/home/user/QubesIncoming/dom0"
REMOTE_OUTPUT_ARCHIVE = "/tmp/kuhbs-repo-output.tar.gz"
REMOTE_WORK_DIR = "/tmp/kuhbs-repo-operation"
# Link, unlink, and linked-repo updates can change policy cached by these long-running daemons
I3_SERVICES = [
    "kuhbs-qvm-autopause.service",
    "kuhbs-workspace-attention.service",
]


def _restart_i3_services(ctx: OperationContext) -> None:
    # XFCE sessions have no live i3 daemons to refresh
    i3_socket = os.environ.get("I3SOCK")
    if not i3_socket or not Path(i3_socket).is_socket():
        return

    # Restart separately so one failure names the stale daemon without undoing the config change
    for service in I3_SERVICES:
        result = ctx.runner.run(
            ["systemctl", "--user", "restart", service],
            source="i3",
            check=False,
        )
        if result.returncode != 0:
            # The config operation succeeded; identify the desktop service that did not restart
            ctx.logger.warning("i3", f"Failed to restart {service}")


def _repo_id_from_url(url: str) -> str:
    # Only https://host/path[/repo][.git] and git@host:path[/repo][.git] are supported
    if url.startswith("https://"):
        remainder = url.removeprefix("https://")
        separator = "/"
    elif url.startswith("git@"):
        remainder = url.removeprefix("git@")
        separator = ":"
    else:
        raise ValueError(f"unsupported repo URL: {url}")
    host, found_separator, path = remainder.partition(separator)
    path = path.removesuffix(".git")
    source = f"{host}/{path}"
    if not found_separator or not REPO_RE.fullmatch(source):
        raise ValueError(f"unsupported repo URL: {url}")
    return source


def _require_repo(source: str) -> str:
    # Repo ids are explicit filesystem paths under ~/.kuhbs/repos, not clone URLs
    if not REPO_RE.fullmatch(source):
        raise ValueError(f"unsupported repo: {source}")
    return source


def _require_kuhb_id(kuhb_id: str) -> str:
    # Unlink removes exactly one local symlink by plain KUHB id
    if not re.fullmatch(r"[A-Za-z0-9_-]+", kuhb_id):
        raise ValueError(f"unsupported kuhb id: {kuhb_id}")
    return kuhb_id


def _require_link(ctx: OperationContext, source: str, *, require_source: bool = True) -> tuple[Path, str]:
    # Link accepts only the repo-relative name shown by repo-list and the GUI
    match = LINK_RE.fullmatch(source)
    if match is None:
        raise ValueError(f"unsupported link target: {source}; use repo/kuhb")
    kuhb_id = _require_kuhb_id(match.group("kuhb"))
    repo_root = _repo_path(ctx, match.group("repo"))
    if not _is_repo_checkout(repo_root):
        raise ValueError(f"repo is not a real git checkout: {repo_root}")
    repo_kuhb = repo_root / kuhb_id
    if require_source and (repo_kuhb.is_symlink() or not repo_kuhb.is_dir()):
        raise ValueError(f"repo KUHB must be a real directory: {repo_kuhb}")
    definition_path = repo_kuhb / "kuhb.yml"
    if require_source and (definition_path.is_symlink() or not definition_path.is_file()):
        raise ValueError(f"repo KUHB definition must be a real file: {definition_path}")
    return repo_kuhb, kuhb_id


def _repo_path(ctx: OperationContext, source: str) -> Path:
    # The local path mirrors the repo id and must stay under ~/.kuhbs/repos
    source = _require_repo(source)
    path = (ctx.repos_root / source).absolute()
    current = ctx.repos_root.absolute()
    for part in Path(source).parts[:-1]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"repo path contains symlink component: {source}")
        if _is_repo_checkout(current):
            raise ValueError(f"repo path is nested inside another checkout: {source}")
    return path


def _is_repo_checkout(path: Path) -> bool:
    # Repos are real directories containing a real .git directory
    git_dir = path / ".git"
    return path.is_dir() and not path.is_symlink() and git_dir.is_dir() and not git_dir.is_symlink()


def _repo_kuhb_dirs(repo_root: Path) -> list[Path]:
    # KUHB definitions are real direct child directories, never symlink escapes from a checkout
    directories = []
    for child in repo_root.iterdir():
        definition = child / "kuhb.yml"
        if child.is_dir() and not child.is_symlink() and definition.is_file() and not definition.is_symlink():
            directories.append(child)
    return sorted(directories)


def _repo_dirs(ctx: OperationContext) -> list[Path]:
    # Existing repos are git checkouts under ~/.kuhbs/repos; host path depth is not fixed
    if not ctx.repos_root.exists():
        return []
    roots = []
    for root, dirs, _files in os.walk(ctx.repos_root):
        root_path = Path(root)
        if ".git" in dirs:
            git_dir = root_path / ".git"
            if not root_path.is_symlink() and not git_dir.is_symlink():
                roots.append(root_path)
            dirs[:] = []
            continue
        dirs[:] = [dirname for dirname in dirs if not dirname.startswith(".")]
        for dirname in list(dirs):
            path = root_path / dirname
            if path.is_symlink():
                dirs.remove(dirname)
    return sorted(roots)


def _validate_repo_definition_set(
    ctx: OperationContext,
    *,
    replacing: Path | None = None,
    candidate: Path | None = None,
) -> None:
    # Repository IDs are global, so add/update must validate every checkout as one definition set.
    roots = [root for root in _repo_dirs(ctx) if replacing is None or root != replacing]
    if candidate is not None:
        roots.append(candidate)
    entries = []
    for repo_root in roots:
        for kuhb_root in _repo_kuhb_dirs(repo_root):
            definition_path = kuhb_root / "kuhb.yml"
            definition = validate_kuhb_file(ctx.defaults, definition_path)
            entries.append((kuhb_root.name, definition_path, definition))
    validate_definition_set(ctx.defaults, entries)


def _remove_path(path: Path) -> None:
    # Delete a file, symlink, or checkout tree at a verified local repo path
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)



def _repo_stage(target: Path) -> Path:
    # Keep the stage beside the target so the final directory rename stays on one filesystem
    target.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f".{target.name}.new-", dir=target.parent))


def _replace_checkout(stage: Path, target: Path) -> None:
    # One dom0 operator accepts the short absent-path window between removal and rename
    if target.is_symlink():
        raise RuntimeError(f"repo target must be a real directory, not a symlink: {target}")
    if target.exists():
        _remove_path(target)
    stage.rename(target)


def _run_repo_in_named_dispvm(
    ctx: OperationContext,
    mode: str,
    source: str,
    url: str,
    branch: str,
    target: Path,
    stage: Path,
    linked_kuhbs: list[str],
) -> None:
    # Four random digits keep concurrent manual terminals recognizable without creating a persistent VM name
    vm_name = f"repo-{mode}-{secrets.randbelow(10000):04d}"
    created = False
    with tempfile.TemporaryDirectory(prefix=f"kuhbs-repo-{mode}-") as temporary_dir:
        temporary = Path(temporary_dir)
        ssh_key = temporary / "id_ed25519"
        input_archive = temporary / "kuhbs-repo-input.tar.gz"
        output_archive = temporary / "kuhbs-repo-output.tar.gz"
        remote_key = f"{REMOTE_INCOMING}/{ssh_key.name}"
        remote_input = f"{REMOTE_INCOMING}/{input_archive.name}" if mode == "update" else ""

        # Keep a real replacement key out of argv and logs while qvm-copy transfers it into the fresh VM
        ssh_key.write_text(ctx.defaults["repos"]["ssh_private_key"], encoding="utf-8")
        ssh_key.chmod(0o600)
        if mode == "update":
            # Update sends the full dirty checkout so the VM can own all Git and merge behavior
            ctx.runner.run_for_dom0(["tar", "-C", str(target), "-czf", str(input_archive), "."])

        try:
            # --disp creates a named DispVM with Qubes' normal red disposable label
            ctx.runner.run_for_kuh(
                vm_name,
                ["qvm-create", "--disp", "--template", ctx.defaults["repos"]["dispvm_template"], vm_name],
            )
            created = True
            copy_paths = [str(ssh_key)]
            if mode == "update":
                copy_paths.append(str(input_archive))
            ctx.runner.run(["qvm-copy-to-vm", vm_name, *copy_paths], source=vm_name)

            # Reuse the classic setup-script terminal so success closes in five seconds and failures stay debuggable
            script_args = [
                mode,
                url,
                branch,
                str(ctx.defaults["repos"]["commit_selection_count"]),
                remote_input,
                REMOTE_OUTPUT_ARCHIVE,
                remote_key,
                REMOTE_WORK_DIR,
                *linked_kuhbs,
            ]
            run_script_in_kuh_terminal(
                ctx,
                source,
                vm_name,
                REPO_OPERATION_SCRIPT,
                user="user",
                script_args=script_args,
            )
            # Repository archives are one of the explicit pass-io exceptions because the destination is dom0
            copy_command = (
                f"qvm-run --pass-io --user user {quote(vm_name)} "
                f"cat {quote(REMOTE_OUTPUT_ARCHIVE)} > {quote(str(output_archive))}"
            )
            ctx.runner.shell_for_dom0(copy_command)
            ctx.runner.run_for_dom0(["tar", "-C", str(stage), "-xzf", str(output_archive)])
            if not _is_repo_checkout(stage):
                raise RuntimeError("repository VM returned an invalid Git checkout")
        finally:
            if created:
                # Wait-for-terminal happens above, so cleanup never destroys a shell the user is still debugging
                ctx.runner.run_for_kuh(vm_name, ["qvm-kill", vm_name], check=False)
                ctx.runner.run_for_kuh(vm_name, ["qvm-remove", "--force", vm_name])


def add_repo(ctx: OperationContext, url: str, *, branch: str | None = None) -> None:
    # Clone one branch, let the user select its audited commit, then import that exact checkout
    source = _repo_id_from_url(url)
    branch = ctx.defaults["repos"]["branch"] if branch is None else branch
    target = _repo_path(ctx, source)
    if target.exists() or target.is_symlink():
        raise RuntimeError(f"repo already exists: {source}; use update-repo or repo-remove first")
    ctx.logger.status(source, f"Cloning {branch}")
    # A new repository has no checkout to protect, so import directly at its final path
    target.mkdir(parents=True)
    try:
        _run_repo_in_named_dispvm(ctx, "add", source, url, branch, target, target, [])
        _validate_repo_definition_set(ctx)
    except BaseException:
        _remove_path(target)
        raise
    ctx.logger.status(source, "Added repo")


def _branch_from_checkout(ctx: OperationContext, target: Path) -> str:
    # A single-branch clone records its selected branch in the origin fetch refspec
    result = ctx.runner.run_for_dom0(
        ["git", "-C", str(target), "config", "--get", "remote.origin.fetch"],
        log=False,
    )
    refspec = result.stdout.strip()
    prefix = "+refs/heads/"
    separator = ":refs/remotes/origin/"
    if not refspec.startswith(prefix) or separator not in refspec:
        raise RuntimeError(f"repo does not record one update branch: {target}")
    branch, remote_branch = refspec[len(prefix):].split(separator, 1)
    if not branch or branch != remote_branch:
        raise RuntimeError(f"repo has an unsupported update branch refspec: {result.stdout.strip()}")
    return branch


def _validate_prospective_definition_set(ctx: OperationContext, replacements: dict[str, Path]) -> None:
    # Validate every changed definition against all independently valid active siblings
    entries: list[tuple[str, Path, dict]] = []
    active_ids: set[str] = set()
    for active_path in local_kuhb_paths(ctx.kuhbs_root):
        active_id = active_path.parent.name
        prospective_path = replacements.get(active_id, active_path)
        try:
            definition = validate_kuhb_file(ctx.defaults, prospective_path)
        except ConfigValidationError:
            if active_id in replacements:
                raise
            continue
        entries.append((active_id, prospective_path, definition))
        active_ids.add(active_id)
    for active_id, prospective_path in replacements.items():
        if active_id in active_ids:
            continue
        definition = validate_kuhb_file(ctx.defaults, prospective_path)
        entries.append((active_id, prospective_path, definition))
    validate_definition_changes(ctx.defaults, entries, set(replacements))


def _validate_staged_linked_kuhbs(ctx: OperationContext, target: Path, stage: Path, kuhb_ids: list[str]) -> None:
    # A staged update substitutes every linked candidate before validating the full active definition set
    target_root = target.resolve()
    replacements: dict[str, Path] = {}
    for kuhb_id in kuhb_ids:
        active = ctx.kuhbs_root / kuhb_id
        relative = active.resolve().relative_to(target_root)
        candidate_dir = stage / relative
        if candidate_dir.is_symlink() or not candidate_dir.is_dir():
            raise ValueError(f"staged linked KUHB must be a real directory: {candidate_dir}")
        replacements[kuhb_id] = candidate_dir / "kuhb.yml"
    _validate_prospective_definition_set(ctx, replacements)


def update_repo(ctx: OperationContext, source: str) -> None:
    # One operator stages and validates the complete replacement before removing the old checkout
    target = _repo_path(ctx, source)
    if not _is_repo_checkout(target):
        raise RuntimeError(f"repo is not a git checkout: {target}")
    branch = _branch_from_checkout(ctx, target)
    linked = _linked_kuhbs_for_repo(ctx, target)
    ctx.logger.status(source, f"Fetching {branch}")

    stage = _repo_stage(target)
    try:
        _run_repo_in_named_dispvm(ctx, "update", source, "", branch, target, stage, linked)
        _validate_repo_definition_set(ctx, replacing=target, candidate=stage)
        _validate_staged_linked_kuhbs(ctx, target, stage, linked)
        _replace_checkout(stage, target)
    except BaseException:
        # The old checkout stays intact until replacement; failed staging has one cleanup owner
        _remove_path(stage)
        raise
    ctx.logger.status(source, "Updated repo")
    if linked:
        _restart_i3_services(ctx)


def list_repos(ctx: OperationContext) -> None:
    # Print installed repo ids in the command format accepted by update-repo and repo-remove
    for path in _repo_dirs(ctx):
        print(path.relative_to(ctx.repos_root).as_posix())


def _linked_kuhbs_for_repo(ctx: OperationContext, repo_root: Path) -> list[str]:
    # Find local My KUHBS symlinks that point inside one repo checkout
    resolved_repo_root = repo_root.resolve()
    if not ctx.kuhbs_root.exists():
        return []
    linked = []
    for child in sorted(ctx.kuhbs_root.iterdir()):
        if child.is_symlink() and child.resolve().is_relative_to(resolved_repo_root):
            linked.append(child.name)
    return linked


def remove_repo(ctx: OperationContext, source: str) -> None:
    # Remove one repo checkout only when no local KUHB symlink still uses it
    target = _repo_path(ctx, source)
    if not target.exists():
        # A typo must not look like a successful repository removal
        raise RuntimeError(f"repo does not exist: {source}")
    linked = _linked_kuhbs_for_repo(ctx, target)
    if linked:
        names = "\n  ".join(linked)
        raise RuntimeError(f"Cannot remove repo {source}\nLinked kuhbs still use it:\n  {names}")
    if target.exists() and not _is_repo_checkout(target):
        raise RuntimeError(f"repo is not a git checkout: {target}")
    _remove_path(target)
    ctx.logger.status(source, "Removed repo")


def link_kuhbs(ctx: OperationContext, sources: list[str]) -> None:
    # Preflight the complete requested set so normal validation errors cannot leave a partial batch
    if not sources:
        raise ValueError("link requires at least one source")
    links: list[tuple[Path, str, Path]] = []
    replacements: dict[str, Path] = {}
    for source in sources:
        repo_kuhb, kuhb_id = _require_link(ctx, source)
        if kuhb_id in replacements:
            raise ValueError(f"duplicate KUHB target: {kuhb_id}")
        target = ctx.kuhbs_root / kuhb_id
        allowed, reason = ctx.state_store.can_i("link", kuhb_id, linked=target.exists() or target.is_symlink())
        if not allowed:
            raise RuntimeError(reason)
        replacements[kuhb_id] = repo_kuhb / "kuhb.yml"
        links.append((repo_kuhb, kuhb_id, target))
    _validate_prospective_definition_set(ctx, replacements)

    ctx.kuhbs_root.mkdir(parents=True, exist_ok=True)
    for repo_kuhb, kuhb_id, target in links:
        target.symlink_to(repo_kuhb)
        ctx.logger.status(kuhb_id, f"Linked {repo_kuhb} -> {target}")
    # Long-running desktop daemons need only one restart after the complete batch is visible
    _restart_i3_services(ctx)


def link_kuhb(ctx: OperationContext, source: str) -> None:
    # Preserve the internal single-source API while the CLI and GUI use the batch operation
    link_kuhbs(ctx, [source])


def unlink_kuhbs(ctx: OperationContext, sources: list[str]) -> None:
    # Verify every source and lifecycle gate before removing the first symlink
    if not sources:
        raise ValueError("unlink requires at least one source")
    links: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for source in sources:
        repo_kuhb, kuhb_id = _require_link(ctx, source, require_source=False)
        if kuhb_id in seen:
            raise ValueError(f"duplicate KUHB target: {kuhb_id}")
        seen.add(kuhb_id)
        target = ctx.kuhbs_root / kuhb_id
        if not target.is_symlink():
            raise RuntimeError(f"local kuhb is not a repo link: {target}")
        if target.resolve() != repo_kuhb.resolve():
            raise RuntimeError(f"local kuhb does not link to {source}: {target}")
        allowed, reason = ctx.state_store.can_i("unlink", kuhb_id, linked=True)
        if not allowed:
            raise RuntimeError(reason)
        links.append((kuhb_id, target))

    for kuhb_id, target in links:
        target.unlink()
        ctx.logger.status(kuhb_id, "Unlinked")
    # One restart applies the final linked set without tripping systemd's start-rate limit
    _restart_i3_services(ctx)


def unlink_kuhb(ctx: OperationContext, source: str) -> None:
    # Preserve the internal single-source API while the CLI and GUI use the batch operation
    unlink_kuhbs(ctx, [source])
