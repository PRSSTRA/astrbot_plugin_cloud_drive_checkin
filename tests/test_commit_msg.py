import subprocess
import tempfile
import unittest
from pathlib import Path


HOOK = Path(__file__).resolve().parents[1] / ".githooks" / "commit-msg"


class CommitMessageTests(unittest.TestCase):
    def check_message(self, message):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as file:
            file.write(message + "\n")
            file.flush()
            return subprocess.run(
                ["bash", str(HOOK), file.name], capture_output=True, text=True
            )

    def test_chinese_description_with_english_scope(self):
        self.assertEqual(self.check_message("构建(ci): 添加 PR 检测工作流").returncode, 0)
        self.assertEqual(self.check_message("fix(api/v2): 修复 404").returncode, 0)

    def test_english_only_description_is_rejected(self):
        result = self.check_message("构建(ci): Update hook")
        self.assertEqual(result.returncode, 1)
        self.assertIn("提交描述须至少包含一个中文汉字", result.stderr)

    def test_scope_and_delimiters_remain_restricted(self):
        for message in ("构建(本地): 更新钩子", "构建(): 更新钩子", "构建（ci）: 更新钩子"):
            with self.subTest(message=message):
                self.assertEqual(self.check_message(message).returncode, 1)


if __name__ == "__main__":
    unittest.main()
