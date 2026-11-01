import os
import subprocess
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
        second_invalid = self.commit(
            "fix(api): Update policy\n\n补充接口策略说明。"
        )

        result = self.run_policy("commits", self.base_sha, second_invalid)

        self.assertEqual(result.returncode, 1)
        self.assertIn(first_invalid[:12], result.stderr)
        self.assertIn(second_invalid[:12], result.stderr)

    def test_tracked_agent_files_are_rejected(self):
        agent_file = self.repo / ".omp" / "agents" / "local.md"
        agent_file.parent.mkdir(parents=True)
        agent_file.write_text("local only\n", encoding="utf-8")
        self.git("add", ".omp/agents/local.md")
        self.git("commit", "--quiet", "-m", VALID_MESSAGE)
        head_sha = self.git("rev-parse", "HEAD")

        result = self.run_policy("commits", self.base_sha, head_sha)

        self.assertEqual(result.returncode, 1)
        self.assertIn(head_sha[:12], result.stderr)
        self.assertIn(".omp/agents/", result.stderr)

    def test_title_mode_uses_the_real_hook(self):
        title_file = self.temp_path / "title.txt"
        title_file.write_text(
            "构建(hooks): 完善工作树与提交校验\n", encoding="utf-8"
        )
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


if __name__ == "__main__":
    unittest.main()
