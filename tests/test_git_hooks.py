import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HOOKS = REPOSITORY_ROOT / ".githooks"
PRE_COMMIT = HOOKS / "pre-commit"
INSTALLER = REPOSITORY_ROOT / "scripts" / "install-git-hooks.sh"
VALID_TITLE = "构建(hooks): 添加提交校验"
VALID_BODY = "补充本地提交历史检查。"


class GitHookTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.main_worktree = self.root / "main"
        self.linked_worktree = self.root / "linked"
        self.home = self.root / "home"
        self.home.mkdir()
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_AUTHOR_NAME": "Hook Test",
                "GIT_AUTHOR_EMAIL": "hook-test@example.invalid",
                "GIT_COMMITTER_NAME": "Hook Test",
                "GIT_COMMITTER_EMAIL": "hook-test@example.invalid",
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "commit.gpgsign",
                "GIT_CONFIG_VALUE_0": "false",
                "GIT_CONFIG_KEY_1": "tag.gpgsign",
                "GIT_CONFIG_VALUE_1": "false",
            }
        )

        self.run_git(
            self.root, "init", "--initial-branch=master", str(self.main_worktree)
        )
        (self.main_worktree / "seed.txt").write_text("seed\n", encoding="utf-8")
        shutil.copyfile(
            REPOSITORY_ROOT / ".gitignore", self.main_worktree / ".gitignore"
        )
        self.run_git(self.main_worktree, "add", "seed.txt", ".gitignore")
        self.run_git(self.main_worktree, "commit", "--no-verify", "-m", "initial")
        self.run_git(
            self.main_worktree,
            "worktree",
            "add",
            "-b",
            "chore/13-hooks",
            str(self.linked_worktree),
            "HEAD",
        )
        self.run_git(self.main_worktree, "switch", "-c", "chore/14-main-worktree")
        self.run_git(
            self.main_worktree,
            "config",
            "--local",
            "core.hooksPath",
            str(HOOKS),
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def run_command(self, command, *, cwd, env=None, check=True):
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env or self.environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(
                f"command failed: {command!r}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def run_git(self, cwd, *arguments, check=True):
        return self.run_command(["git", *arguments], cwd=cwd, check=check)

    def run_pre_commit(self, cwd):
        return self.run_command(["bash", str(PRE_COMMIT)], cwd=cwd, check=False)

    def stage_file(self, cwd, relative_path, content="change\n"):
        path = cwd / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.run_git(cwd, "add", relative_path.as_posix())

    def test_real_commit_passes_in_linked_worktree_and_main_worktree_is_blocked(self):
        self.stage_file(self.linked_worktree, Path("linked.txt"))
        linked_result = self.run_git(
            self.linked_worktree,
            "commit",
            "-m",
            VALID_TITLE,
            "-m",
            VALID_BODY,
            check=False,
        )
        self.assertEqual(linked_result.returncode, 0, linked_result.stderr)

        self.stage_file(self.main_worktree, Path("main.txt"))
        main_result = self.run_git(
            self.main_worktree,
            "commit",
            "-m",
            VALID_TITLE,
            "-m",
            VALID_BODY,
            check=False,
        )
        self.assertNotEqual(main_result.returncode, 0)
        self.assertIn("主工作目录", main_result.stderr)

    def test_merge_state_does_not_bypass_protected_branch(self):
        self.run_git(self.linked_worktree, "switch", "-c", "dev/0.0.1")
        merge_head = self.run_git(
            self.linked_worktree, "rev-parse", "--git-path", "MERGE_HEAD"
        ).stdout.strip()
        merge_head_path = Path(merge_head)
        if not merge_head_path.is_absolute():
            merge_head_path = self.linked_worktree / merge_head_path
        current_head = self.run_git(
            self.linked_worktree, "rev-parse", "HEAD"
        ).stdout.strip()
        merge_head_path.write_text(f"{current_head}\n", encoding="ascii")

        result = self.run_pre_commit(self.linked_worktree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("保护分支", result.stderr)

    def test_detached_head_is_rejected(self):
        self.run_git(self.linked_worktree, "switch", "--detach")
        result = self.run_pre_commit(self.linked_worktree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("detached HEAD", result.stderr)

    def test_nonstandard_branch_warns_but_release_branch_is_recognized(self):
        self.run_git(self.linked_worktree, "switch", "-c", "experiment/hooks")
        warning_result = self.run_pre_commit(self.linked_worktree)
        self.assertEqual(warning_result.returncode, 0, warning_result.stderr)
        self.assertIn("警告", warning_result.stderr)

        self.run_git(self.linked_worktree, "switch", "-c", "release/0.0.1")
        release_result = self.run_pre_commit(self.linked_worktree)
        self.assertEqual(release_result.returncode, 0, release_result.stderr)
        self.assertNotIn("警告", release_result.stderr)

    def test_untracked_local_files_do_not_block_but_forced_tracking_does(self):
        local_file = self.linked_worktree / ".omp" / "session.txt"
        local_file.parent.mkdir()
        local_file.write_text("local only\n", encoding="utf-8")
        allowed_result = self.run_pre_commit(self.linked_worktree)
        self.assertEqual(allowed_result.returncode, 0, allowed_result.stderr)

        self.run_git(self.linked_worktree, "add", "-f", ".omp/session.txt")
        blocked_result = self.run_pre_commit(self.linked_worktree)
        self.assertNotEqual(blocked_result.returncode, 0)
        self.assertIn(".omp/", blocked_result.stderr)
        self.run_git(self.linked_worktree, "rm", "--cached", ".omp/session.txt")
        self.assertEqual(self.run_pre_commit(self.linked_worktree).returncode, 0)

    def test_installer_is_cwd_independent_idempotent_and_repository_local(self):
        fixture_hooks = self.main_worktree / ".githooks"
        fixture_scripts = self.main_worktree / "scripts"
        fixture_hooks.mkdir()
        fixture_scripts.mkdir()
        fixture_installer = fixture_scripts / INSTALLER.name
        shutil.copyfile(INSTALLER, fixture_installer)
        shutil.copyfile(
            REPOSITORY_ROOT / "scripts" / "check-repository-content.py",
            fixture_scripts / "check-repository-content.py",
        )
        copied_hooks = []
        for source in HOOKS.iterdir():
            destination = fixture_hooks / source.name
            shutil.copyfile(source, destination)
            destination.chmod(0o600)
            copied_hooks.append(destination)
        original_contents = {path.name: path.read_bytes() for path in copied_hooks}
        outside = self.root / "outside"
        outside.mkdir()

        for _ in range(2):
            result = self.run_command(
                [shutil.which("bash"), str(fixture_installer)], cwd=outside
            )
            self.assertIn(str(self.main_worktree.resolve()), result.stdout)

        configured_path = self.run_git(
            self.main_worktree, "config", "--local", "--get", "core.hooksPath"
        ).stdout.strip()
        self.assertEqual(configured_path, ".githooks")
        for path in copied_hooks:
            self.assertEqual(path.read_bytes(), original_contents[path.name])
            self.assertTrue(path.stat().st_mode & stat.S_IXUSR)

    def test_installer_rejects_missing_python(self):
        fixture_scripts = self.main_worktree / "scripts"
        fixture_scripts.mkdir()
        fixture_installer = fixture_scripts / INSTALLER.name
        shutil.copyfile(INSTALLER, fixture_installer)
        empty_path = self.root / "empty-path"
        empty_path.mkdir()
        environment = self.environment.copy()
        environment["PATH"] = str(empty_path)

        result = self.run_command(
            [shutil.which("bash"), str(fixture_installer)],
            cwd=self.root,
            env=environment,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("python3", result.stderr)

    def test_installer_uses_its_own_repository_not_calling_directory(self):
        outside = self.root / "outside-repository" / "scripts"
        outside.mkdir(parents=True)
        copied_installer = outside / INSTALLER.name
        shutil.copyfile(INSTALLER, copied_installer)

        result = self.run_command(
            [shutil.which("bash"), str(copied_installer)],
            cwd=self.main_worktree,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不在 Git 仓库", result.stderr)


if __name__ == "__main__":
    unittest.main()
