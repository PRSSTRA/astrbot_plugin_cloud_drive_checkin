#!/usr/bin/env python3
"""Check Git snapshots, not working-tree secrets or generated file contents."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


class PolicyError(Exception):
    pass


def git(root: Path, *arguments: str, env: dict[str, str] | None = None) -> bytes:
    result = subprocess.run(
        ["git", *arguments], cwd=root, env=env, capture_output=True, check=False
    )
    if result.returncode:
        raise PolicyError("无法读取 Git 提交树或暂存区；未放行提交。")
    return result.stdout


def entry(root: Path, path: str, env: dict[str, str]) -> tuple[bytes, bytes] | None:
    raw = git(root, "ls-files", "--stage", "--full-name", "--", path, env=env)
    if not raw:
        return None
    records = raw.splitlines()
    if len(records) != 1:
        raise PolicyError("提交树存在未合并条目；未放行提交。")
    header = records[0].split(b"\t", 1)[0].split()
    if len(header) != 3 or header[2] != b"0":
        raise PolicyError("提交树存在未合并条目；未放行提交。")
    return header[0], header[1]


def rules(root: Path, env: dict[str, str]) -> bytes:
    record = entry(root, ".gitignore", env)
    if record is None or record[0] not in (b"100644", b"100755"):
        raise PolicyError("暂存区必须包含普通文件 .gitignore 作为入库路径规则。")
    return git(root, "show", ":.gitignore", env=env)


def check_tianyi_defaults(root: Path, env: dict[str, str]) -> None:
    path = "third_party/tianyi/index.py"
    record = entry(root, path, env)
    if record is None:
        return
    if record[0] not in (b"100644", b"100755"):
        raise PolicyError(f"{path} 必须是普通源码文件。")
    if (
        int(git(root, "cat-file", "-s", record[1].decode("ascii"), env=env))
        > 1024 * 1024
    ):
        raise PolicyError(f"{path} 超过执行器支持的源码大小。")
    try:
        tree = ast.parse(git(root, "show", ":" + path, env=env))
        expected = {"accounts": [], "TG_BOT_TOKEN": "", "TG_CHAT_ID": ""}
        seen: set[str] = set()
        for statement in tree.body:
            if isinstance(statement, ast.Assign):
                targets, value = statement.targets, statement.value
            elif isinstance(statement, ast.AnnAssign):
                targets, value = [statement.target], statement.value
            else:
                continue
            for target in targets:
                if not isinstance(target, ast.Name) or target.id not in expected:
                    continue
                name = target.id
                actual = ast.literal_eval(value)
                if (
                    name in seen
                    or type(actual) is not type(expected[name])
                    or actual != expected[name]
                ):
                    raise ValueError
                seen.add(name)
        if seen != set(expected):
            raise ValueError
    except (SyntaxError, ValueError, TypeError, RecursionError):
        raise PolicyError(
            f"{path} 的 accounts、TG_BOT_TOKEN、TG_CHAT_ID 声明必须各出现一次且为空；不输出配置值。"
        ) from None


def check(mode: str, revision: str | None) -> int:
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
    environment = os.environ.copy()
    # Use the real/staged policy even when checking an earlier PR commit.
    policy = rules(root, environment)
    with tempfile.TemporaryDirectory(prefix="check-repository-content-") as temporary:
        directory = Path(temporary)
        ignore_file = directory / "rules"
        ignore_file.write_bytes(policy)
        if mode == "tree":
            if revision is None or re.fullmatch(r"[0-9a-fA-F]{40}", revision) is None:
                raise PolicyError("tree 模式需要完整的 40 位提交 SHA。")
            git(root, "cat-file", "-e", revision + "^{commit}")
            environment["GIT_INDEX_FILE"] = str(directory / "index")
            git(root, "read-tree", revision, env=environment)
        blocked = git(
            root,
            "ls-files",
            "--cached",
            "--ignored",
            "--full-name",
            "-z",
            "--exclude-from=" + str(ignore_file),
            env=environment,
        ).split(b"\0")
        failed = False
        for name in blocked:
            if name:
                print(
                    "错误：提交树跟踪了根 .gitignore 禁止入库的路径："
                    + repr(os.fsdecode(name)),
                    file=sys.stderr,
                )
                failed = True
        try:
            check_tianyi_defaults(root, environment)
        except PolicyError as error:
            print("错误：" + str(error), file=sys.stderr)
            failed = True
        return int(failed)


def main() -> int:
    arguments = sys.argv[1:]
    if arguments != ["index"] and not (len(arguments) == 2 and arguments[0] == "tree"):
        print(
            "用法：check-repository-content.py index | tree COMMIT_SHA", file=sys.stderr
        )
        return 2
    try:
        return check(arguments[0], arguments[1] if len(arguments) == 2 else None)
    except (PolicyError, OSError) as error:
        message = (
            str(error)
            if isinstance(error, PolicyError)
            else "无法运行入库检查；未放行提交。"
        )
        print("错误：" + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
