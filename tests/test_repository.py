# Purpose: Regression tests for KUHBS git repository linking
# Scope: Uses temp Git repos, the real VM-side script, and explicit fake Qubes runners
import os
import socket
from contextlib import redirect_stdout
import io
from pathlib import Path
from unittest.mock import patch
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from helpers import MappingRunner, RecordingEventLogger as EventLogger, RunnerContext
from kuhbs.operations import OperationContext
from kuhbs.operations import repository
from kuhbs.validation import ConfigValidationError
from helpers import current_defaults


def context(tmp_path: Path) -> OperationContext:
    # The classic terminal runner must exist because repository operations reuse the setup-script path
    runner = tmp_path / "usr/share/kuhbs/setup-scripts/kuhbs/kuhbs-run-script.sh"
    runner.parent.mkdir(parents=True)
    runner.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    defaults = {
        "paths": {
            "config": str(tmp_path / ".kuhbs"),
            "kuhbs": str(tmp_path / ".kuhbs/my-kuhbs"),
            "setup_scripts": str(tmp_path / "usr/share/kuhbs/setup-scripts"),
        },
        "repos": {"branch": "main"},
    }
    logger = EventLogger(log_path=tmp_path / "kuhbs.log", color=False, stdout=False)
    return RunnerContext(current_defaults(defaults), logger, MappingRunner(logger))


def git(repo: Path, *args: str) -> str:
    # Real temporary repositories verify the merge behavior that Git itself owns
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True)
    return result.stdout.strip()


def repo_operation_script() -> Path:
    # Tests execute the shipped script directly because Qubes command fakes cannot model its Git behavior
    return Path(__file__).resolve().parents[1] / "install/templates/usr/share/kuhbs/kuhbs-repo-operation.sh"


def run_repo_operation_script(tmp_path: Path, *args: str, user_input: str) -> subprocess.CompletedProcess[str]:
    # Each fake VM gets its own HOME so the copied example SSH key never touches the test user's real files
    home = tmp_path / "vm-home"
    home.mkdir(exist_ok=True)
    environment = dict(os.environ)
    environment["HOME"] = str(home)
    return subprocess.run(
        ["bash", "-e", str(repo_operation_script()), *args],
        input=user_input,
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )


def write_valid_kuhb(root: Path, kuhb_id: str, *, instance_id: str = "default") -> None:
    # Link tests use a complete candidate because repository linking now runs the product validator
    (root / "icon.svg").write_text("<svg/>", encoding="utf-8")
    (root / "kuhb.yml").write_text(
        f"""id: {kuhb_id}
name: {kuhb_id}
description: Repository test KUHB
icon: icon.svg
order: 500
type: app
template: debian-13-minimal
kuhs:
  app:
    instances:
      - id: {instance_id}
""",
        encoding="utf-8",
    )


class RepositoryTests(unittest.TestCase):
    def test_i3_service_restart_skips_non_i3_session(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = context(Path(td))
            restart = getattr(repository, "_restart_i3_services", None)
            self.assertTrue(callable(restart))
            environment = dict(os.environ)
            environment.pop("I3SOCK", None)

            with patch.dict(os.environ, environment, clear=True):
                with patch.object(ctx.runner, "run") as run:
                    restart(ctx)

            run.assert_not_called()

    def test_i3_service_restart_logs_failure_without_failing_operation(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            socket_path = tmp_path / "i3.sock"
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(socket_path))
                with patch.dict(os.environ, {"I3SOCK": str(socket_path)}):
                    with patch.object(
                        ctx.runner,
                        "run",
                        side_effect=[
                            SimpleNamespace(returncode=1),
                            SimpleNamespace(returncode=0),
                        ],
                    ) as run:
                        repository._restart_i3_services(ctx)

            self.assertEqual(
                [entry.args[0] for entry in run.call_args_list],
                [
                    ["systemctl", "--user", "restart", repository.I3_SERVICES[0]],
                    ["systemctl", "--user", "restart", repository.I3_SERVICES[1]],
                ],
            )
            self.assertTrue(
                all(entry.kwargs == {"source": "i3", "check": False} for entry in run.call_args_list)
            )
            self.assertIn(
                f"Failed to restart {repository.I3_SERVICES[0]}",
                (tmp_path / "kuhbs.log").read_text(encoding="utf-8"),
            )

    def test_repo_add_accepts_https_and_ssh_git_urls(self):
        self.assertEqual(repository._repo_id_from_url("https://github.com/foo/bar"), "github.com/foo/bar")
        self.assertEqual(repository._repo_id_from_url("https://github.com/foo/bar.git"), "github.com/foo/bar")
        self.assertEqual(repository._repo_id_from_url("git@github.com:foo/bar"), "github.com/foo/bar")
        self.assertEqual(repository._repo_id_from_url("git@github.com:foo/bar.git"), "github.com/foo/bar")
        self.assertEqual(repository._repo_id_from_url("https://git.me.com/foo/bar/qux/bla/foobar.git"), "git.me.com/foo/bar/qux/bla/foobar")

    def test_repo_add_rejects_non_git_url_shapes(self):
        with self.assertRaises(ValueError):
            repository._repo_id_from_url("github.com/foo/bar")
        with self.assertRaises(ValueError):
            repository._repo_id_from_url("ssh://git@github.com/foo/bar.git")

    def test_repo_ids_reject_dots_in_owner_and_project_paths(self):
        for source in (
            "https://github.com/.foo/bar",
            "https://github.com/foo/bar.name",
            "git@github.com:foo.name/bar.git",
        ):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    repository._repo_id_from_url(source)

        with self.assertRaises(ValueError):
            repository._require_repo("github.com/foo/bar.name")

    def test_repo_add_uses_named_dispvm_and_classic_vm_terminal_runner(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)

            # Command-shape coverage trusts the fake VM archive instead of requiring a real returned checkout
            with patch.object(repository.secrets, "randbelow", return_value=42), \
                 patch.object(
                     repository,
                     "_is_repo_checkout",
                     side_effect=lambda path: Path(path).name == "bar" or Path(path).name.startswith(".bar.new-"),
                 ):
                repository.add_repo(ctx, "git@github.com:foo/bar.git", branch="master")

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("qvm-create --disp --template default-dvm repo-add-0042", log_text)
            self.assertIn("qvm-copy-to-vm repo-add-0042", log_text)
            self.assertIn("qvm-run --quiet --user user repo-add-0042", log_text)
            self.assertIn("kuhbs-repo-operation.sh add git@github.com:foo/bar.git master", log_text)
            self.assertIn("qvm-kill repo-add-0042", log_text)
            self.assertIn("qvm-remove --force repo-add-0042", log_text)

    def test_repo_add_imports_directly_into_the_new_checkout_path(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = context(Path(td))

            def fake_import(_ctx, _mode, _source, _url, _branch, target, destination, _linked):
                self.assertEqual(destination, target)
                (destination / ".git").mkdir(parents=True)

            with patch.object(repository, "_run_repo_in_named_dispvm", side_effect=fake_import):
                repository.add_repo(ctx, "https://github.com/foo/bar")

            self.assertTrue((ctx.repos_root / "github.com/foo/bar/.git").is_dir())

    def test_repo_add_rejects_duplicate_kuhb_ids_across_repositories(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = context(Path(td))
            existing = ctx.repos_root / "github.com/first/repo"
            (existing / ".git").mkdir(parents=True)
            (existing / "shared").mkdir()
            write_valid_kuhb(existing / "shared", "shared")

            def fake_import(_ctx, _mode, _source, _url, _branch, target, destination, _linked):
                self.assertEqual(destination, target)
                (destination / ".git").mkdir(parents=True)
                (destination / "shared").mkdir()
                write_valid_kuhb(destination / "shared", "shared")

            with patch.object(repository, "_run_repo_in_named_dispvm", side_effect=fake_import):
                with self.assertRaisesRegex(ConfigValidationError, "duplicate KUHB id shared"):
                    repository.add_repo(ctx, "https://github.com/second/repo")

            self.assertFalse((ctx.repos_root / "github.com/second/repo").exists())

    def test_update_repo_uses_named_dispvm_from_configured_template(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            (ctx.repos_root / "github.com/foo/bar/.git").mkdir(parents=True)

            # Command-shape coverage supplies branch/import results that the fake VM cannot archive
            with patch.object(repository.secrets, "randbelow", return_value=7), \
                 patch.object(repository, "_branch_from_checkout", return_value="main"), \
                 patch.object(
                     repository,
                     "_is_repo_checkout",
                     side_effect=lambda path: Path(path).name == "bar" or Path(path).name.startswith(".bar.new-"),
                 ):
                repository.update_repo(ctx, "github.com/foo/bar")

            log_text = (tmp_path / "kuhbs.log").read_text()
            self.assertIn("qvm-create --disp --template default-dvm repo-update-0007", log_text)
            self.assertIn("qvm-run --quiet --user user repo-update-0007", log_text)
            self.assertIn("kuhbs-repo-operation.sh update", log_text)
            self.assertIn("qvm-remove --force repo-update-0007", log_text)

    def test_repo_update_rejects_duplicate_kuhb_ids_across_repositories(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            target = ctx.repos_root / "github.com/first/repo"
            (target / ".git").mkdir(parents=True)
            (target / "current").mkdir()
            write_valid_kuhb(target / "current", "current")
            other = ctx.repos_root / "github.com/other/repo"
            (other / ".git").mkdir(parents=True)
            (other / "shared").mkdir()
            write_valid_kuhb(other / "shared", "shared")
            stage = tmp_path / "stage"
            (stage / ".git").mkdir(parents=True)
            (stage / "shared").mkdir()
            write_valid_kuhb(stage / "shared", "shared")

            with patch.object(repository, "_branch_from_checkout", return_value="main"), \
                 patch.object(repository, "_repo_stage", return_value=stage), \
                 patch.object(repository, "_run_repo_in_named_dispvm"):
                with self.assertRaisesRegex(ConfigValidationError, "duplicate KUHB id shared"):
                    repository.update_repo(ctx, "github.com/first/repo")

            self.assertTrue((target / "current/kuhb.yml").is_file())
            self.assertFalse(stage.exists())

    def test_repo_vm_script_add_selects_full_commit_and_returns_complete_checkout(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            origin = tmp_path / "origin.git"
            work = tmp_path / "work"
            subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(origin)], check=True)
            subprocess.run(["git", "clone", "--quiet", str(origin), str(work)], check=True)
            git(work, "config", "user.name", "KUHBS Test")
            git(work, "config", "user.email", "kuhbs@example.invalid")
            (work / "README.md").write_text("base\n", encoding="utf-8")
            git(work, "add", ".")
            git(work, "commit", "--quiet", "-m", "base")
            base = git(work, "rev-parse", "HEAD")
            (work / "README.md").write_text("newer\n", encoding="utf-8")
            git(work, "commit", "--quiet", "-am", "newer")
            git(work, "push", "--quiet", "origin", "main")
            key = tmp_path / "id_ed25519"
            key.write_text("example key\n", encoding="utf-8")
            output_archive = tmp_path / "output.tar.gz"

            result = run_repo_operation_script(
                tmp_path,
                "add",
                str(origin),
                "main",
                "10",
                "",
                str(output_archive),
                str(key),
                str(tmp_path / "vm-work"),
                user_input="2\n",
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(base, result.stdout)
            checkout = tmp_path / "checkout"
            checkout.mkdir()
            subprocess.run(["tar", "-C", str(checkout), "-xzf", str(output_archive)], check=True)
            self.assertEqual(git(checkout, "rev-parse", "HEAD"), base)
            self.assertTrue((checkout / ".git").is_dir())
            self.assertEqual((tmp_path / "vm-home/.ssh/id_ed25519").stat().st_mode & 0o777, 0o600)

    def test_update_branch_comes_from_single_branch_clone_refspec(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            repo_root = ctx.repos_root / "github.com/foo/bar"
            repo_root.mkdir(parents=True)
            subprocess.run(["git", "-C", str(repo_root), "init", "--quiet"], check=True)
            git(repo_root, "config", "remote.origin.fetch", "+refs/heads/master:refs/remotes/origin/master")
            # The explicit fake runner returns the same refspec written into the temporary checkout
            command = ("git", "-C", str(repo_root), "config", "--get", "remote.origin.fetch")
            ctx.runner.stdout[command] = git(repo_root, "config", "--get", "remote.origin.fetch")

            self.assertEqual(repository._branch_from_checkout(ctx, repo_root), "master")

    def test_repo_vm_script_update_rebases_commits_and_reapplies_all_worktree_changes(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            origin = tmp_path / "origin.git"
            work = tmp_path / "work"
            subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(origin)], check=True)
            subprocess.run(["git", "clone", "--quiet", str(origin), str(work)], check=True)
            git(work, "config", "user.name", "KUHBS Test")
            git(work, "config", "user.email", "kuhbs@example.invalid")
            kuhb_yml = work / "signal/kuhb.yml"
            kuhb_yml.parent.mkdir()
            middle = "\n".join(f"setting-{number}: value" for number in range(10))
            kuhb_yml.write_text(f"description: Signal\n{middle}\nnetvm: sys-net\n", encoding="utf-8")
            (work / ".gitignore").write_text("signal/configs/*\n", encoding="utf-8")
            git(work, "add", ".")
            git(work, "commit", "--quiet", "-m", "base")
            base = git(work, "rev-parse", "HEAD")
            kuhb_yml.write_text(f"description: Signal Desktop\n{middle}\nnetvm: sys-net\n", encoding="utf-8")
            git(work, "commit", "--quiet", "-am", "description")
            git(work, "push", "--quiet", "origin", "main")

            target = tmp_path / "target"
            subprocess.run(["git", "clone", "--quiet", str(origin), str(target)], check=True)
            git(target, "checkout", "--quiet", "--detach", base)
            git(target, "config", "user.name", "KUHBS Test")
            git(target, "config", "user.email", "kuhbs@example.invalid")
            target_yml = target / "signal/kuhb.yml"
            local_script = target / "signal/scripts/tpl/90-local.sh"
            local_script.parent.mkdir(parents=True)
            local_script.write_text("printf 'local\\n'\n", encoding="utf-8")
            git(target, "add", str(local_script.relative_to(target)))
            git(target, "commit", "--quiet", "-m", "local setup script")
            target_yml.write_text(f"description: Signal\n{middle}\nnetvm: ndp-custom\n", encoding="utf-8")
            ignored_config = target / "signal/configs/private.conf"
            ignored_config.parent.mkdir(parents=True)
            ignored_config.write_text("local private config\n", encoding="utf-8")
            untracked = target / "signal/local-notes.txt"
            untracked.write_text("local notes\n", encoding="utf-8")
            (work / "README.md").write_text("new audited release\n", encoding="utf-8")
            git(work, "add", ".")
            git(work, "commit", "--quiet", "-m", "new release")
            selected = git(work, "rev-parse", "HEAD")
            git(work, "push", "--quiet", "origin", "main")

            input_archive = tmp_path / "input.tar.gz"
            subprocess.run(["tar", "-C", str(target), "-czf", str(input_archive), "."], check=True)
            key = tmp_path / "id_ed25519"
            key.write_text("example key\n", encoding="utf-8")
            output_archive = tmp_path / "output.tar.gz"

            result = run_repo_operation_script(
                tmp_path,
                "update",
                "",
                "main",
                "10",
                str(input_archive),
                str(output_archive),
                str(key),
                str(tmp_path / "vm-work"),
                user_input="1\ny\n",
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Repository Update Review", result.stdout)
            self.assertIn(f"Selected upstream commit: {selected}", result.stdout)
            self.assertIn("Final HEAD:", result.stdout)
            self.assertIn("Status:", result.stdout)
            self.assertIn("Recent history:", result.stdout)
            self.assertIn("Diff stat:", result.stdout)
            self.assertIn("Tracked diff:", result.stdout)
            self.assertIn("Apply this Repository Update to dom0? [y/N]", result.stdout)
            updated = tmp_path / "updated"
            updated.mkdir()
            subprocess.run(["tar", "-C", str(updated), "-xzf", str(output_archive)], check=True)
            self.assertEqual(git(updated, "merge-base", "HEAD", selected), selected)
            self.assertIn("local setup script", git(updated, "log", "--format=%s", f"{selected}..HEAD"))
            self.assertIn("netvm: ndp-custom", (updated / "signal/kuhb.yml").read_text(encoding="utf-8"))
            self.assertEqual((updated / "signal/scripts/tpl/90-local.sh").read_text(encoding="utf-8"), "printf 'local\\n'\n")
            self.assertEqual((updated / "signal/configs/private.conf").read_text(encoding="utf-8"), "local private config\n")
            self.assertEqual((updated / "signal/local-notes.txt").read_text(encoding="utf-8"), "local notes\n")

    def test_repo_vm_script_update_rejects_uppercase_final_approval(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            origin = tmp_path / "origin.git"
            work = tmp_path / "work"
            target = tmp_path / "target"
            subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(origin)], check=True)
            subprocess.run(["git", "clone", "--quiet", str(origin), str(work)], check=True)
            git(work, "config", "user.name", "KUHBS Test")
            git(work, "config", "user.email", "kuhbs@example.invalid")
            (work / "signal").mkdir()
            (work / "signal/kuhb.yml").write_text("description: old\n", encoding="utf-8")
            git(work, "add", ".")
            git(work, "commit", "--quiet", "-m", "base")
            git(work, "push", "--quiet", "origin", "main")
            subprocess.run(["git", "clone", "--quiet", str(origin), str(target)], check=True)
            (work / "signal/kuhb.yml").write_text("description: new\n", encoding="utf-8")
            git(work, "commit", "--quiet", "-am", "update signal")
            selected = git(work, "rev-parse", "HEAD")
            git(work, "push", "--quiet", "origin", "main")
            input_archive = tmp_path / "input.tar.gz"
            subprocess.run(["tar", "-C", str(target), "-czf", str(input_archive), "."], check=True)
            key = tmp_path / "id_ed25519"
            key.write_text("example key\n", encoding="utf-8")
            output_archive = tmp_path / "output.tar.gz"

            result = run_repo_operation_script(
                tmp_path,
                "update",
                "",
                "main",
                "10",
                str(input_archive),
                str(output_archive),
                str(key),
                str(tmp_path / "vm-work"),
                "signal",
                user_input="1\nY\n",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("This update changes linked KUHBs", result.stdout)
            self.assertIn("signal", result.stdout)
            self.assertIn("Apply this Repository Update to dom0? [y/N]", result.stdout)
            self.assertFalse(output_archive.exists())

    def test_repo_list_reads_git_checkout_dirs_at_any_depth(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            repo_root = ctx.repos_root / "github.com/foo/bar"
            (repo_root / ".git").mkdir(parents=True)
            deep_repo = ctx.repos_root / "git.me.com/foo/bar/qux/bla/foobar"
            (deep_repo / ".git").mkdir(parents=True)

            output = io.StringIO()
            with redirect_stdout(output):
                repository.list_repos(ctx)

            self.assertEqual(output.getvalue().splitlines(), ["git.me.com/foo/bar/qux/bla/foobar", "github.com/foo/bar"])

    def test_repo_list_ignores_staging_dirs_and_symlinked_repo_roots(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            real_repo = tmp_path / "checkout"
            (real_repo / ".git").mkdir(parents=True)
            (ctx.repos_root / "github.com/foo").mkdir(parents=True)
            (ctx.repos_root / "github.com/foo/bar").symlink_to(real_repo, target_is_directory=True)
            staging = ctx.repos_root / "github.com/foo/.bar.new"
            (staging / ".git").mkdir(parents=True)
            nested = ctx.repos_root / "github.com/foo/baz"
            (nested / ".git").mkdir(parents=True)
            (nested / "outside").symlink_to(real_repo, target_is_directory=True)
            fake_git = ctx.repos_root / "github.com/foo/qux"
            fake_git.mkdir(parents=True)
            (fake_git / ".git").symlink_to(real_repo / ".git", target_is_directory=True)

            output = io.StringIO()
            with redirect_stdout(output):
                repository.list_repos(ctx)

            self.assertEqual(output.getvalue().splitlines(), ["github.com/foo/baz"])

    def test_repo_path_rejects_symlinked_parent_components(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            outside = tmp_path / "outside"
            outside.mkdir()
            ctx.repos_root.mkdir(parents=True)
            (ctx.repos_root / "github.com").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlink"):
                repository._repo_path(ctx, "github.com/foo/bar")

    def test_repo_path_rejects_nested_checkout(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = context(Path(td))
            parent_repo = ctx.repos_root / "github.com/foo"
            (parent_repo / ".git").mkdir(parents=True)

            with self.assertRaisesRegex(ValueError, "nested"):
                repository._repo_path(ctx, "github.com/foo/bar")

    def test_link_rejects_symlinked_kuhb_directory(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            repo_root = ctx.repos_root / "github.com/foo/bar"
            (repo_root / ".git").mkdir(parents=True)
            outside = tmp_path / "outside-browser"
            outside.mkdir()
            (outside / "kuhb.yml").write_text("id: browser\nkuhs: {}\n", encoding="utf-8")
            (repo_root / "browser").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "real directory"):
                repository.link_kuhb(ctx, "github.com/foo/bar/browser")

            self.assertFalse((ctx.kuhbs_root / "browser").exists())

    def test_link_rejects_symlinked_kuhb_definition(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            repo_root = ctx.repos_root / "github.com/foo/bar"
            repo_kuhb = repo_root / "browser"
            (repo_root / ".git").mkdir(parents=True)
            repo_kuhb.mkdir()
            outside = tmp_path / "outside-kuhb.yml"
            outside.write_text("id: browser\nkuhs: {}\n", encoding="utf-8")
            (repo_kuhb / "kuhb.yml").symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "real file"):
                repository.link_kuhb(ctx, "github.com/foo/bar/browser")

            self.assertFalse((ctx.kuhbs_root / "browser").exists())

    def test_link_validates_candidate_before_creating_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            repo_kuhb = ctx.repos_root / "github.com/foo/bar/browser"
            repo_kuhb.mkdir(parents=True)
            (repo_kuhb.parent / ".git").mkdir()
            (repo_kuhb / "kuhb.yml").write_text("id: browser\nkuhs: {}\n", encoding="utf-8")

            with self.assertRaises(ConfigValidationError):
                repository.link_kuhb(ctx, "github.com/foo/bar/browser")

            self.assertFalse((ctx.kuhbs_root / "browser").exists())

    def test_update_of_linked_repo_restarts_i3_services(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            target = ctx.repos_root / "github.com/foo/bar"
            active_kuhb = target / "browser"
            active_kuhb.mkdir(parents=True)
            (target / ".git").mkdir()
            write_valid_kuhb(active_kuhb, "browser")
            ctx.kuhbs_root.mkdir(parents=True)
            (ctx.kuhbs_root / "browser").symlink_to(active_kuhb)
            stage = tmp_path / "stage"
            (stage / "browser").mkdir(parents=True)
            write_valid_kuhb(stage / "browser", "browser")

            with patch.object(repository, "_branch_from_checkout", return_value="main"):
                with patch.object(repository, "_repo_stage", return_value=stage):
                    with patch.object(repository, "_run_repo_in_named_dispvm"):
                        with patch.object(repository, "_restart_i3_services") as restart:
                            repository.update_repo(ctx, "github.com/foo/bar")

            restart.assert_called_once_with(ctx)

    def test_update_validates_staged_linked_candidate_before_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            target = ctx.repos_root / "github.com/foo/bar"
            stage = tmp_path / "stage"
            active_kuhb = target / "browser"
            staged_kuhb = stage / "browser"
            active_kuhb.mkdir(parents=True)
            staged_kuhb.mkdir(parents=True)
            write_valid_kuhb(active_kuhb, "browser")
            (staged_kuhb / "kuhb.yml").write_text("id: browser\nkuhs: {}\n", encoding="utf-8")
            ctx.kuhbs_root.mkdir(parents=True)
            (ctx.kuhbs_root / "browser").symlink_to(active_kuhb)

            with self.assertRaises(ConfigValidationError):
                repository._validate_staged_linked_kuhbs(ctx, target, stage, ["browser"])

    def test_link_validates_the_full_prospective_definition_set(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            active = ctx.kuhbs_root / "alpha"
            active.mkdir(parents=True)
            write_valid_kuhb(active, "alpha", instance_id="beta")
            candidate = ctx.repos_root / "github.com/foo/bar/alpha-beta"
            candidate.mkdir(parents=True)
            (candidate.parent / ".git").mkdir()
            write_valid_kuhb(candidate, "alpha-beta")

            with self.assertRaises(ConfigValidationError) as cm:
                repository.link_kuhb(ctx, "github.com/foo/bar/alpha-beta")

            self.assertIn("duplicate resolved KUH name app-alpha-beta", str(cm.exception))
            self.assertFalse((ctx.kuhbs_root / "alpha-beta").exists())

    def test_link_valid_candidate_ignores_unrelated_broken_active_definition(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            broken = ctx.kuhbs_root / "broken"
            broken.mkdir(parents=True)
            (broken / "kuhb.yml").write_text("{}\n", encoding="utf-8")
            candidate = ctx.repos_root / "github.com/foo/bar/good"
            candidate.mkdir(parents=True)
            (candidate.parent / ".git").mkdir()
            write_valid_kuhb(candidate, "good")

            repository.link_kuhb(ctx, "github.com/foo/bar/good")

            linked = ctx.kuhbs_root / "good"
            self.assertTrue(linked.is_symlink())
            self.assertEqual(linked.resolve(), candidate)

    def test_update_validates_the_full_prospective_definition_set(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            target = ctx.repos_root / "github.com/foo/bar"
            stage = tmp_path / "stage"
            active = ctx.kuhbs_root / "alpha"
            linked = target / "alpha-beta"
            staged = stage / "alpha-beta"
            active.mkdir(parents=True)
            linked.mkdir(parents=True)
            staged.mkdir(parents=True)
            write_valid_kuhb(active, "alpha", instance_id="beta")
            write_valid_kuhb(linked, "alpha-beta", instance_id="other")
            write_valid_kuhb(staged, "alpha-beta")
            (ctx.kuhbs_root / "alpha-beta").symlink_to(linked)

            with self.assertRaises(ConfigValidationError) as cm:
                repository._validate_staged_linked_kuhbs(ctx, target, stage, ["alpha-beta"])

            self.assertIn("duplicate resolved KUH name app-alpha-beta", str(cm.exception))

    def test_existing_checkout_replacement_deletes_old_checkout_then_renames_stage(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            target = tmp_path / "target"
            stage = tmp_path / "stage"
            target.mkdir()
            stage.mkdir()
            (target / "value").write_text("old\n", encoding="utf-8")
            (stage / "value").write_text("new\n", encoding="utf-8")

            with patch.object(repository, "_remove_path", wraps=repository._remove_path) as remove_path:
                repository._replace_checkout(stage, target)

            remove_path.assert_called_once_with(target)
            self.assertEqual((target / "value").read_text(encoding="utf-8"), "new\n")
            self.assertFalse(stage.exists())
            self.assertFalse((target / "value").is_symlink())

    def test_batch_link_and_unlink_restart_i3_services_once_per_batch(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            repo = ctx.repos_root / "github.com/foo/bar"
            (repo / ".git").mkdir(parents=True)
            sources = []
            for kuhb_id in ("alpha", "bravo"):
                candidate = repo / kuhb_id
                candidate.mkdir()
                write_valid_kuhb(candidate, kuhb_id)
                sources.append(f"github.com/foo/bar/{kuhb_id}")

            with patch.object(repository, "_restart_i3_services") as restart:
                repository.link_kuhbs(ctx, sources)
                restart.assert_called_once_with(ctx)
                restart.reset_mock()
                repository.unlink_kuhbs(ctx, sources)
                restart.assert_called_once_with(ctx)

            self.assertFalse((ctx.kuhbs_root / "alpha").exists())
            self.assertFalse((ctx.kuhbs_root / "bravo").exists())

    def test_batch_link_rejects_invalid_last_source_before_creating_any_link(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            repo = ctx.repos_root / "github.com/foo/bar"
            (repo / ".git").mkdir(parents=True)
            alpha = repo / "alpha"
            broken = repo / "broken"
            alpha.mkdir()
            broken.mkdir()
            write_valid_kuhb(alpha, "alpha")
            (broken / "kuhb.yml").write_text("id: broken\nkuhs: {}\n", encoding="utf-8")

            with patch.object(repository, "_restart_i3_services") as restart:
                with self.assertRaises(ConfigValidationError):
                    repository.link_kuhbs(
                        ctx,
                        ["github.com/foo/bar/alpha", "github.com/foo/bar/broken"],
                    )

            self.assertFalse((ctx.kuhbs_root / "alpha").exists())
            self.assertFalse((ctx.kuhbs_root / "broken").exists())
            restart.assert_not_called()

    def test_batch_link_validates_the_complete_set_before_creating_links(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            repo = ctx.repos_root / "github.com/foo/bar"
            (repo / ".git").mkdir(parents=True)
            alpha = repo / "alpha"
            alpha_beta = repo / "alpha-beta"
            alpha.mkdir()
            alpha_beta.mkdir()
            write_valid_kuhb(alpha, "alpha", instance_id="beta")
            write_valid_kuhb(alpha_beta, "alpha-beta")

            with self.assertRaises(ConfigValidationError):
                repository.link_kuhbs(
                    ctx,
                    ["github.com/foo/bar/alpha", "github.com/foo/bar/alpha-beta"],
                )

            self.assertFalse((ctx.kuhbs_root / "alpha").exists())
            self.assertFalse((ctx.kuhbs_root / "alpha-beta").exists())

    def test_batch_link_rejects_duplicate_target_before_creating_links(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            sources = []
            for repo_id in ("one", "two"):
                repo = ctx.repos_root / f"github.com/foo/{repo_id}"
                (repo / ".git").mkdir(parents=True)
                candidate = repo / "browser"
                candidate.mkdir()
                write_valid_kuhb(candidate, "browser")
                sources.append(f"github.com/foo/{repo_id}/browser")

            with self.assertRaisesRegex(ValueError, "duplicate KUHB target: browser"):
                repository.link_kuhbs(ctx, sources)

            self.assertFalse((ctx.kuhbs_root / "browser").exists())

    def test_batch_unlink_preflights_every_source_before_removing_links(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            repo = ctx.repos_root / "github.com/foo/bar"
            (repo / ".git").mkdir(parents=True)
            for kuhb_id in ("alpha", "bravo"):
                candidate = repo / kuhb_id
                candidate.mkdir()
                write_valid_kuhb(candidate, kuhb_id)
            ctx.kuhbs_root.mkdir(parents=True)
            (ctx.kuhbs_root / "alpha").symlink_to(repo / "alpha")
            wrong = tmp_path / "wrong-bravo"
            wrong.mkdir()
            (ctx.kuhbs_root / "bravo").symlink_to(wrong)

            with self.assertRaisesRegex(RuntimeError, "does not link"):
                repository.unlink_kuhbs(
                    ctx,
                    ["github.com/foo/bar/alpha", "github.com/foo/bar/bravo"],
                )

            self.assertTrue((ctx.kuhbs_root / "alpha").is_symlink())
            self.assertTrue((ctx.kuhbs_root / "bravo").is_symlink())

    def test_link_and_unlink_restart_i3_services(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            repo_kuhb = ctx.repos_root / "github.com/foo/bar/browser"
            repo_kuhb.mkdir(parents=True)
            (repo_kuhb.parent / ".git").mkdir()
            write_valid_kuhb(repo_kuhb, "browser")

            with patch.object(repository, "_restart_i3_services") as restart:
                repository.link_kuhb(ctx, "github.com/foo/bar/browser")
                repository.unlink_kuhb(ctx, "github.com/foo/bar/browser")

            self.assertEqual(restart.call_count, 2)
            self.assertTrue(all(entry.args == (ctx,) for entry in restart.call_args_list))

    def test_link_and_unlink_use_symlink_only_when_absent(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            repo_kuhb = ctx.repos_root / "github.com/foo/bar/browser"
            repo_kuhb.mkdir(parents=True)
            (repo_kuhb.parent / ".git").mkdir()
            write_valid_kuhb(repo_kuhb, "browser")

            repository.link_kuhb(ctx, "github.com/foo/bar/browser")

            target = ctx.kuhbs_root / "browser"
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), repo_kuhb)
            repository.unlink_kuhb(ctx, "github.com/foo/bar/browser")
            self.assertFalse(target.exists())

    def test_link_rejects_absolute_repo_paths(self):
        # User input is always the repo-relative target shown by repo-list and GUI
        with tempfile.TemporaryDirectory() as td:
            ctx = context(Path(td))
            absolute = ctx.repos_root / "github.com/foo/bar/browser"

            with self.assertRaisesRegex(ValueError, "unsupported link target"):
                repository.link_kuhb(ctx, str(absolute))

    def test_link_rejects_directory_that_does_not_match_kuhb_id(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            repo_kuhb = ctx.repos_root / "github.com/foo/bar/browser"
            repo_kuhb.mkdir(parents=True)
            (repo_kuhb.parent / ".git").mkdir()
            write_valid_kuhb(repo_kuhb, "surf")

            with self.assertRaises(ValueError):
                repository.link_kuhb(ctx, "github.com/foo/bar/browser")

    def test_unlink_refuses_created_kuhb_until_resources_are_removed(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            repo_kuhb = ctx.repos_root / "github.com/foo/bar/browser"
            repo_kuhb.mkdir(parents=True)
            (repo_kuhb.parent / ".git").mkdir()
            write_valid_kuhb(repo_kuhb, "browser")
            repository.link_kuhb(ctx, "github.com/foo/bar/browser")
            ctx.set_state("browser", "create", "completed")

            with self.assertRaisesRegex(RuntimeError, "state is create:completed"):
                repository.unlink_kuhb(ctx, "github.com/foo/bar/browser")

            # A completed remove proves KUHBS-owned VM resources were cleaned before the link disappears
            ctx.set_state("browser", "remove", "completed")
            repository.unlink_kuhb(ctx, "github.com/foo/bar/browser")
            self.assertFalse((ctx.kuhbs_root / "browser").exists())

    def test_unlink_refuses_when_operation_is_running(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            repo_kuhb = ctx.repos_root / "github.com/foo/bar/browser"
            repo_kuhb.mkdir(parents=True)
            (repo_kuhb.parent / ".git").mkdir()
            write_valid_kuhb(repo_kuhb, "browser")
            repository.link_kuhb(ctx, "github.com/foo/bar/browser")
            ctx.set_state("browser", "backup", "start")

            with self.assertRaises(RuntimeError):
                repository.unlink_kuhb(ctx, "github.com/foo/bar/browser")

    def test_repo_remove_refuses_linked_kuhbs(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            repo_kuhb = ctx.repos_root / "github.com/foo/bar/browser"
            repo_kuhb.mkdir(parents=True)
            (repo_kuhb.parent / ".git").mkdir()
            write_valid_kuhb(repo_kuhb, "browser")
            repository.link_kuhb(ctx, "github.com/foo/bar/browser")

            with self.assertRaises(RuntimeError):
                repository.remove_repo(ctx, "github.com/foo/bar")

    def test_repo_add_refuses_existing_checkout_before_clone(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            repo_root = ctx.repos_root / "github.com/foo/bar"
            (repo_root / ".git").mkdir(parents=True)

            with self.assertRaisesRegex(RuntimeError, "repo already exists"):
                repository.add_repo(ctx, "https://github.com/foo/bar")

    def test_repo_remove_refuses_non_git_subdirectories(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            nested = ctx.repos_root / "github.com/foo/bar/browser"
            nested.mkdir(parents=True)

            with self.assertRaisesRegex(RuntimeError, "repo is not a git checkout"):
                repository.remove_repo(ctx, "github.com/foo/bar/browser")

            self.assertTrue(nested.exists())

    def test_repo_remove_refuses_missing_repo(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = context(Path(td))

            with self.assertRaisesRegex(RuntimeError, "repo does not exist"):
                repository.remove_repo(ctx, "github.com/foo/missing")

    def test_repo_remove_rejects_symlinked_repo_roots(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ctx = context(tmp_path)
            real_repo = tmp_path / "checkout"
            (real_repo / ".git").mkdir(parents=True)
            repo_link = ctx.repos_root / "github.com/foo/bar"
            repo_link.parent.mkdir(parents=True)
            repo_link.symlink_to(real_repo, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "repo is not a git checkout"):
                repository.remove_repo(ctx, "github.com/foo/bar")

    def test_repo_paths_reject_dotdot_components(self):
        with self.assertRaises(ValueError):
            repository._repo_id_from_url("https://github.com/foo/../bar.git")
        with self.assertRaises(ValueError):
            repository.update_repo(context(Path(tempfile.mkdtemp())), "github.com/foo/../bar")

    def test_unlink_rejects_path_components(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = context(Path(td))
            with self.assertRaises(ValueError):
                repository.unlink_kuhb(ctx, "../browser")


if __name__ == "__main__":
    unittest.main()
