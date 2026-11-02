import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "scripts" / "check-pr-policy.sh"

VALID_MESSAGE = "构建(hooks): 添加提交校验\n\n补充本地提交历史检查。"
SECOND_VALID_MESSAGE = "fix(api/v2): 修复 404 响应\n\n修复接口缺失资源时的响应。"


class PullRequestPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.temp_path = Path(self.temp_dir.name)
        self.repo = self.temp_path / "repo"
        self.repo.mkdir()
        self.runtime_tmp = self.temp_path / "tmp"
        self.runtime_tmp.mkdir()
        home = self.temp_path / "home"
        home.mkdir()

        self.git_env = os.environ.copy()
        for variable in tuple(self.git_env):
            if variable.startswith("GIT_"):
                self.git_env.pop(variable)
        self.git_env.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "HOME": str(home),
                "TMPDIR": str(self.runtime_tmp),
                "XDG_CONFIG_HOME": str(home / ".config"),
            }
        )
        self.git("init", "--quiet", "--initial-branch=dev/0.0.1")
        self.git("config", "user.name", "Policy Test")
        self.git("config", "user.email", "policy@example.invalid")
        (self.repo / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
        self.git("add", ".gitignore")
        self.base_sha = self.commit("不符合新规范的历史基线")

    def git(self, *arguments):
        return subprocess.run(
            ["git", *arguments],
            cwd=self.repo,
            env=self.git_env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit(self, message):
        self.git("commit", "--allow-empty", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD")

    def run_policy(self, *arguments):
        return subprocess.run(
            ["bash", str(POLICY), *arguments],
            cwd=self.repo,
            env=self.git_env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_old_baseline_is_ignored_and_legal_commits_pass(self):
        head_sha = self.commit(VALID_MESSAGE)

        result = self.run_policy("commits", self.base_sha, head_sha)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.runtime_tmp.iterdir()), [])

    def test_one_illegal_message_in_multiple_commits_fails(self):
        self.commit(VALID_MESSAGE)
        invalid_sha = self.commit("构建(hooks): 缺少正文")
        head_sha = self.commit(SECOND_VALID_MESSAGE)

        result = self.run_policy("commits", self.base_sha, head_sha)

        self.assertEqual(result.returncode, 1)
        self.assertIn(invalid_sha[:12], result.stderr)
        self.assertNotIn(self.base_sha[:12], result.stderr)

    def test_every_illegal_commit_is_reported(self):
        first_invalid = self.commit("构建(hooks): 缺少正文")
        second_invalid = self.commit("fix(api): Update policy\n\n补充接口策略说明。")

        result = self.run_policy("commits", self.base_sha, second_invalid)

        self.assertEqual(result.returncode, 1)
        self.assertIn(first_invalid[:12], result.stderr)
        self.assertIn(second_invalid[:12], result.stderr)

    def test_tracked_agent_files_are_rejected(self):
        agent_file = self.repo / ".omp" / "agents" / "local.md"
        agent_file.parent.mkdir(parents=True)
        agent_file.write_text("local only\n", encoding="utf-8")
        self.git("add", "-f", ".omp/agents/local.md")
        self.git("commit", "--quiet", "-m", VALID_MESSAGE)
        head_sha = self.git("rev-parse", "HEAD")

        result = self.run_policy("commits", self.base_sha, head_sha)

        self.assertEqual(result.returncode, 1)
        self.assertIn(head_sha[:12], result.stderr)
        self.assertIn(".omp/agents/", result.stderr)

    def test_title_mode_uses_the_real_hook(self):
        title_file = self.temp_path / "title.txt"
        title_file.write_text("构建(hooks): 完善工作树与提交校验\n", encoding="utf-8")
        valid = self.run_policy("title", str(title_file))

        title_file.write_text("构建(hooks): Update policy\n", encoding="utf-8")
        invalid = self.run_policy("title", str(title_file))

        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertEqual(invalid.returncode, 1)

    def test_master_accepts_only_release_and_hotfix_sources(self):
        for head_branch in ("release/0.0.1", "hotfix/14-urgent-fix"):
            with self.subTest(head_branch=head_branch):
                result = self.run_policy("branch", "master", head_branch)
                self.assertEqual(result.returncode, 0, result.stderr)

        result = self.run_policy("branch", "master", "chore/13-git-policy")
        self.assertEqual(result.returncode, 1)

    def test_dev_accepts_tasks_rejects_protected_sources_and_warns(self):
        task = self.run_policy("branch", "dev/0.0.1", "chore/13-git-policy")
        self.assertEqual(task.returncode, 0, task.stderr)

        for head_branch in ("master", "main", "develop", "dev/0.0.2"):
            with self.subTest(head_branch=head_branch):
                result = self.run_policy("branch", "dev/0.0.1", head_branch)
                self.assertEqual(result.returncode, 1)

        warning = self.run_policy("branch", "dev/0.0.1", "topic/local-check")
        self.assertEqual(warning.returncode, 0, warning.stderr)
        self.assertIn("警告", warning.stderr)

    def test_unknown_base_branch_is_rejected(self):
        result = self.run_policy("branch", "feature/other", "chore/13-git-policy")

        self.assertEqual(result.returncode, 1)
        self.assertIn("不支持的目标分支", result.stderr)

    def content_check(self):
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "check-repository-content.py"),
                "index",
            ],
            cwd=self.repo,
            env=self.git_env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_forced_generated_paths_are_rejected_even_from_a_subdirectory(self):
        paths = [
            "bundle.pyz",
            "deps/wheel.whl",
            "legacy.zip",
            ".env.local",
            "credentials.json",
            "master-key.env",
            "pkg/__pycache__/cached.pyc",
            ".venv/bin/python",
            "node_modules/pkg/index.js",
            "odd\nname.pyz",
        ]
        for name in paths:
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fake fixture only\n", encoding="utf-8")
            self.git("add", "-f", "--", name)
        subdirectory = self.repo / "subdir"
        subdirectory.mkdir()
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "check-repository-content.py"),
                "index",
            ],
            cwd=subdirectory,
            env=self.git_env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        for name in paths:
            self.assertIn(repr(name), result.stderr)

    def test_removed_artifact_is_still_rejected_in_pr_history(self):
        artifact = self.repo / "bundle.pyz"
        artifact.write_bytes(b"fake artifact")
        self.git("add", "-f", "bundle.pyz")
        introduced = self.commit(VALID_MESSAGE)
        self.git("rm", "bundle.pyz")
        removed = self.commit(SECOND_VALID_MESSAGE)
        self.assertEqual(self.content_check().returncode, 0)
        result = self.run_policy("commits", self.base_sha, removed)
        self.assertEqual(result.returncode, 1)
        self.assertIn(introduced[:12], result.stderr)

    def test_unstaged_ignore_changes_do_not_relax_staged_policy(self):
        (self.repo / "credentials.json").write_text("{}", encoding="utf-8")
        self.git("add", "-f", "credentials.json")
        (self.repo / ".gitignore").write_text("", encoding="utf-8")
        self.assertNotEqual(self.content_check().returncode, 0)

    def test_source_licenses_locks_and_sanitized_env_examples_remain_allowed(self):
        for name in (
            "third_party/example/LICENSE",
            "third_party/example/requirements.lock",
            "tests/test_fixture.py",
            ".env.example",
        ):
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("sanitized fixture\n", encoding="utf-8")
            self.git("add", "--", name)
        (self.repo / ".git" / "info" / "exclude").write_text(
            "tests/\n", encoding="utf-8"
        )
        result = self.content_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_tianyi_configuration_is_checked_without_executing_or_echoing_it(self):
        path = self.repo / "third_party" / "tianyi" / "index.py"
        path.parent.mkdir(parents=True)
        empty = "accounts = []\nTG_BOT_TOKEN = ''\nTG_CHAT_ID = ''\n"
        path.write_text(empty, encoding="utf-8")
        self.git("add", "third_party/tianyi/index.py")
        self.assertEqual(self.content_check().returncode, 0)
        for name, value in (
            ("accounts", "[{'username': 'FAKE_VALUE'}]"),
            ("TG_BOT_TOKEN", "'FAKE_VALUE'"),
            ("TG_CHAT_ID", "'FAKE_VALUE'"),
        ):
            with self.subTest(name=name):
                lines = {"accounts": "[]", "TG_BOT_TOKEN": "''", "TG_CHAT_ID": "''"}
                lines[name] = value
                path.write_text(
                    "\n".join(key + " = " + val for key, val in lines.items()),
                    encoding="utf-8",
                )
                self.git("add", "third_party/tianyi/index.py")
                result = self.content_check()
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("FAKE_VALUE", result.stdout + result.stderr)
        marker = self.repo / "executed"
        path.write_text(
            "accounts = open('executed', 'w')\nTG_BOT_TOKEN = ''\nTG_CHAT_ID = ''\n",
            encoding="utf-8",
        )
        self.git("add", "third_party/tianyi/index.py")
        self.assertNotEqual(self.content_check().returncode, 0)
        self.assertFalse(marker.exists())

    def test_missing_or_symlinked_policy_file_is_not_accepted(self):
        self.git("rm", ".gitignore")
        self.assertNotEqual(self.content_check().returncode, 0)
        external = self.temp_path / "outside-ignore"
        external.write_text("", encoding="utf-8")
        (self.repo / ".gitignore").symlink_to(external)
        self.git("add", ".gitignore")
        self.assertNotEqual(self.content_check().returncode, 0)


if __name__ == "__main__":
    unittest.main()
