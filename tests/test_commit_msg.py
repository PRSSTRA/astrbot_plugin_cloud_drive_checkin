import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


HOOK = Path(__file__).resolve().parents[1] / ".githooks" / "commit-msg"
ALLOWED_TYPES = (
    "功能",
    "修复",
    "文档",
    "样式",
    "重构",
    "性能",
    "测试",
    "构建",
    "持续集成",
    "杂项",
    "回滚",
    "feat",
    "fix",
    "docs",
    "style",
    "refactor",
    "perf",
    "test",
    "build",
    "ci",
    "chore",
    "revert",
)
BODY = "补充本地提交历史检查。"


class CommitMessageTests(unittest.TestCase):
    def check_message(self, message, *, title_only=False, env=None):
        with tempfile.TemporaryDirectory() as directory:
            message_path = Path(directory) / "message"
            message_path.write_text(message, encoding="utf-8")
            command = ["bash", str(HOOK)]
            if title_only:
                command.append("--title-only")
            command.append(str(message_path))
            return subprocess.run(
                command,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

    def full_message(self, title, body=BODY):
        return f"{title}\n\n{body}\n"

    def assert_accepted(self, message, *, title_only=False):
        result = self.check_message(message, title_only=title_only)
        self.assertEqual(result.returncode, 0, result.stderr)

    def assert_rejected(self, message, *, title_only=False):
        result = self.check_message(message, title_only=title_only)
        self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_all_existing_chinese_and_english_types_remain_accepted(self):
        for message_type in ALLOWED_TYPES:
            with self.subTest(message_type=message_type):
                self.assert_accepted(
                    self.full_message(f"{message_type}(hooks): 添加提交校验")
                )

    def test_scope_keeps_existing_ascii_pattern(self):
        for scope in ("a", "api/v2.0_test-hooks"):
            with self.subTest(scope=scope):
                self.assert_accepted(self.full_message(f"构建({scope}): 更新 API2 校验"))
        for scope in ("本地", "Hooks", ".hooks", ""):
            with self.subTest(scope=scope):
                self.assert_rejected(self.full_message(f"构建({scope}): 更新提交校验"))

    def test_subject_unicode_length_boundaries(self):
        self.assert_accepted(self.full_message("修复(api): 修复"))
        self.assert_accepted(self.full_message(f"修复(api): {'修' * 50}"))
        self.assert_rejected(self.full_message("修复(api): 修"))
        self.assert_rejected(self.full_message(f"修复(api): {'修' * 51}"))

    def test_subject_requires_chinese_but_allows_technical_english(self):
        self.assert_accepted(self.full_message("构建(hooks): 更新 API2 hook 校验"))
        self.assert_rejected(self.full_message("构建(hooks): Update API hook"))

    def test_subject_rejects_bad_edges_and_delimiters(self):
        for title in (
            "构建(hooks): ，更新校验",
            "构建(hooks):  更新校验",
            "构建(hooks): 更新校验。",
            "构建(hooks): 更新校验 ",
            "构建（hooks）: 更新校验",
            "构建(hooks)： 更新校验",
            "构建(hooks):更新校验",
        ):
            with self.subTest(title=title):
                self.assert_rejected(self.full_message(title))

    def test_body_accepts_one_or_two_chinese_sentences_with_identifiers(self):
        title = "构建(hooks): 添加提交校验"
        self.assert_accepted(self.full_message(title, "补充 API2 提交检查。"))
        self.assert_accepted(
            self.full_message(title, "补充 API2 提交检查！修复 hook 安装说明？")
        )
        self.assert_accepted(
            self.full_message(title, "补充 API2 提交检查。\n\n修复安装说明。\n\n")
        )

    def test_body_requires_blank_separator_and_one_or_two_sentences(self):
        title = "构建(hooks): 添加提交校验"
        for message in (
            f"{title}\n",
            f"{title}\n\n",
            f"{title}\n{BODY}",
            self.full_message(title, "第一句。第二句！第三句？"),
            self.full_message(title, "正文没有正确结句,"),
            self.full_message(title, "API ready。"),
        ):
            with self.subTest(message=message):
                self.assert_rejected(message)

    def test_title_only_uses_the_same_title_rules_without_requiring_body(self):
        self.assert_accepted(
            "构建(hooks): 完善工作树与提交校验", title_only=True
        )
        self.assert_accepted(
            "构建(hooks): 完善工作树与提交校验\n不检查正文", title_only=True
        )
        self.assert_rejected("构建(hooks): Update hooks", title_only=True)
        self.assert_rejected("未知(hooks): 完善提交校验", title_only=True)

    def test_missing_python_reports_prerequisite_and_blocks_commit(self):
        with tempfile.TemporaryDirectory() as path:
            env = os.environ.copy()
            env["PATH"] = path
            result = subprocess.run(
                [shutil.which("bash"), str(HOOK), "unused"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("python3", result.stderr)



if __name__ == "__main__":
    unittest.main()
