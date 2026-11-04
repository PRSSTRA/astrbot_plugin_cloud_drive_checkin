"""在沙箱内分阶段安装依赖和执行脚本，输出固定 JSON 结果。

File/process safety adapted from dev a662b766 (repository license).
"""
from __future__ import annotations

import codecs
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
from typing import Any

_MAX_JOB_BYTES = 64 * 1024
_MAX_CREDENTIAL_BYTES = 512 * 1024
_MAX_SCRIPT_BYTES = 1024 * 1024
_MAX_ENV_FIELDS = 64
_MAX_ENV_FIELD_BYTES = 16 * 1024
_MAX_ENV_TOTAL_BYTES = 64 * 1024
_ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z', re.ASCII)
_RESERVED_ENV_NAMES = frozenset(
    {'PATH', 'HOME', 'SHELL', 'ENV', 'BASH_ENV', 'IFS'})
_RESERVED_ENV_PREFIXES = ('PYTHON', 'NODE_', 'LD_', 'DYLD_')
_ERROR_CODES = {
    None, 'runtime_unavailable', 'install_error', 'install_timeout',
    'script_error', 'script_timeout', 'execution_unknown', 'shutdown'
}
_RESULT_FIELDS = {
    'protocol', 'stage', 'process_status', 'exit_code', 'duration_ms', 'log',
    'log_truncated', 'error_code'
}


class _RunnerError(Exception):
    pass


class _RuntimeUnavailable(_RunnerError):
    pass


def validate_result(value, stage=None):
    """校验 runner 结果协议并返回副本。

    Args:
        value: 待校验的结果对象。
        stage: 预期阶段；为 None 时接受任一合法阶段。

    Raises:
        ValueError: 字段、状态、体积或日志内容不符合协议。
    """
    if (not isinstance(value, dict) or set(value) != _RESULT_FIELDS or
            type(value['protocol']) is not int or value['protocol'] != 1 or
            value['stage'] not in ('install', 'run') or
        (stage is not None and value['stage'] != stage) or
            value['process_status'] not in ('exited', 'timeout', 'start_error',
                                            'interrupted') or
            type(value['duration_ms']) is not int or
            not 0 <= value['duration_ms'] <= 300000 or
            not isinstance(value['log'], str) or
            type(value['log_truncated']) is not bool or
        (value['error_code'] is not None and
         not isinstance(value['error_code'], str)) or
            value['error_code'] not in _ERROR_CODES):
        raise ValueError('invalid runner result')
    code = value['exit_code']
    if ((value['process_status'] == 'exited' and
         (type(code) is not int or not -(2**31) <= code < 2**31)) or
        (value['process_status'] != 'exited' and code is not None)):
        raise ValueError('invalid runner result')
    try:
        if (len(value['log'].encode('utf-8')) > 4096 or
                len(json.dumps(value,
                               ensure_ascii=False).encode('utf-8')) >= 65536 or
                any(ord(c) < 32 and c not in '\n\t' for c in value['log']) or
                '\x7f' in value['log']):
            raise ValueError('invalid runner result')
    except UnicodeError:
        raise ValueError('invalid runner result') from None
    return dict(value)


class _Normalizer:
    """跨数据块移除终端控制序列及不可展示字符。"""

    def __init__(self):
        self.state = ''

    def feed(self, text):
        output = []
        for char in text:
            if self.state == 'esc':
                self.state = ('csi'
                              if char == '[' else 'osc' if char == ']' else '')
            elif self.state == 'csi':
                if '@' <= char <= '~':
                    self.state = ''
            elif self.state == 'osc':
                if char == '\x07':
                    self.state = ''
                elif char == '\x1b':
                    self.state = 'osc_esc'
            elif self.state == 'osc_esc':
                self.state = '' if char == '\\' else 'osc'
            elif char == '\x1b':
                self.state = 'esc'
            elif char in '\n\t' or (ord(char) >= 32 and ord(char) != 127):
                output.append(char)
        return ''.join(output)


class _LogCollector:
    """跨块脱敏输出，仅保留末尾 4096 字节的可展示日志。"""

    _keys = re.compile(
        r'(?i)\b(?:cookie|authorization|token|password|passwd|secret|access[_-]?key)'
        r'\s*[:=]\s*[^\n]*')

    def __init__(self, secrets=()):
        self.secrets = sorted(
            {v for raw in secrets for v in (raw, _Normalizer().feed(raw)) if v},
            key=len,
            reverse=True)
        # 保留未定尾部，防止凭据或敏感字段跨块时提前泄露。
        self.keep = max([32768] + [len(s) for s in self.secrets])
        self.streams = {
            name: {
                'decoder': codecs.getincrementaldecoder('utf-8')('replace'),
                'normalizer': _Normalizer(),
                'pending': '',
                'suppress': False
            } for name in ('stdout', 'stderr')
        }
        self.log = ''
        self.truncated = False

    def _append(self, name, text):
        if not text:
            return
        self.log += '[' + name + '] ' + text
        encoded = self.log.encode('utf-8')
        if len(encoded) > 4096:
            self.log = encoded[-4096:].decode('utf-8', 'ignore')
            self.truncated = True

    def _flush(self, name, final=False):
        stream = self.streams[name]
        text = stream['pending']
        cutoff = len(text) if final else max(0, len(text) - self.keep)
        if not cutoff:
            return
        matches = [(m.start(), m.end()) for m in self._keys.finditer(text)]
        for secret in self.secrets:
            start = 0
            while True:
                start = text.find(secret, start)
                if start < 0:
                    break
                matches.append((start, start + len(secret)))
                start += len(secret)
        matches.sort()
        merged = []
        for start, end in matches:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        for start, end in merged:
            if start < cutoff < end:
                cutoff = start
                break
        # 超长敏感行不无限缓存，丢弃到换行后再继续收集。
        if not final and cutoff == 0 and len(
                text) > self.keep * 2 and merged and merged[0][0] == 0:
            self._append(name, '[REDACTED]')
            stream['pending'] = ''
            stream['suppress'] = '\n' not in text
            self.truncated = True
            return
        # 收尾时丢弃疑似凭据前缀，避免截断的输出泄露部分凭据。
        if final:
            for secret in self.secrets:
                for length in range(min(len(secret) - 1, len(text)), 0, -1):
                    if text.endswith(secret[:length]):
                        cutoff = min(cutoff, len(text) - length)
                        self.truncated = True
                        break
        result = []
        position = 0
        for start, end in merged:
            if end <= cutoff:
                result.extend((text[position:start], '[REDACTED]'))
                position = end
        result.append(text[position:cutoff])
        self._append(name, ''.join(result))
        stream['pending'] = '' if final else text[cutoff:]

    def add(self, name, chunk):
        stream = self.streams[name]
        text = stream['normalizer'].feed(stream['decoder'].decode(chunk))
        if stream['suppress']:
            if '\n' not in text:
                return
            text = text[text.index('\n'):]
            stream['suppress'] = False
        stream['pending'] += text
        self._flush(name)

    def finish(self):
        for name, stream in self.streams.items():
            stream['pending'] += stream['normalizer'].feed(
                stream['decoder'].decode(b'', final=True))
            self._flush(name, True)
        return self.log, self.truncated


class _Subreaper:
    """在 Linux 支持时接管孤儿后代，清理时回收僵尸进程。"""

    _PR_SET_CHILD_SUBREAPER = 36
    _PR_GET_CHILD_SUBREAPER = 37

    def __init__(self) -> None:
        self._libc: Any | None = None
        self._previous = 0
        self._changed = False

    def enable(self) -> None:
        if not sys.platform.startswith("linux"):
            return
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            previous = ctypes.c_int()
            if (libc.prctl(
                    self._PR_GET_CHILD_SUBREAPER,
                    ctypes.byref(previous),
                    0,
                    0,
                    0,
            ) != 0):
                return
            self._libc = libc
            self._previous = previous.value
            if (previous.value == 0 and
                    libc.prctl(self._PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0):
                self._changed = True
        except (AttributeError, OSError, TypeError, ValueError):
            self._libc = None

    def restore(self) -> None:
        if self._changed and self._libc is not None:
            try:
                self._libc.prctl(
                    self._PR_SET_CHILD_SUBREAPER,
                    self._previous,
                    0,
                    0,
                    0,
                )
            except (AttributeError, OSError, TypeError, ValueError):
                pass


def _json_object(raw: bytes) -> dict[str, object]:

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError
            result[key] = item
        return result

    def reject_constant(_value: str) -> object:
        raise ValueError

    try:
        decoded = raw.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError,
            json.JSONDecodeError):
        raise _RunnerError from None
    if not isinstance(value, dict):
        raise _RunnerError
    return value


def _check_directory(directory: Path) -> int:
    """校验私有目录，并通过 inode 比对拒绝打开期间的目录替换。"""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = os.lstat(directory)
        if (not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode) or
                before.st_uid != os.geteuid() or
                stat.S_IMODE(before.st_mode) != 0o700):
            raise _RunnerError
        directory_fd = os.open(directory, flags)
        opened = os.fstat(directory_fd)
        if (not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.geteuid() or
                stat.S_IMODE(opened.st_mode) != 0o700 or
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            os.close(directory_fd)
            raise _RunnerError
        return directory_fd
    except _RunnerError:
        raise
    except (OSError, ValueError):
        raise _RunnerError from None


def _open_checked_file(directory_fd: int, name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        file_fd = os.open(name, flags, dir_fd=directory_fd)
        file_stat = os.fstat(file_fd)
        if (not stat.S_ISREG(file_stat.st_mode) or
                file_stat.st_uid != os.geteuid() or
                stat.S_IMODE(file_stat.st_mode) != 0o600 or
                file_stat.st_nlink != 1):
            os.close(file_fd)
            raise _RunnerError
        return file_fd
    except _RunnerError:
        raise
    except (OSError, ValueError):
        raise _RunnerError from None


def _read_checked_file(directory_fd: int, name: str, maximum: int) -> bytes:
    file_fd = _open_checked_file(directory_fd, name)
    data = bytearray()
    try:
        file_stat = os.fstat(file_fd)
        if file_stat.st_size > maximum:
            raise _RunnerError
        while len(data) <= maximum:
            chunk = os.read(file_fd, min(64 * 1024, maximum + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > maximum:
            raise _RunnerError
        return bytes(data)
    except _RunnerError:
        raise
    except (OSError, OverflowError, ValueError):
        raise _RunnerError from None
    finally:
        try:
            os.close(file_fd)
        except OSError:
            pass


def _consume_credentials(directory_fd: int) -> bytes:
    """读取一次性凭据，无论读取成功与否都尝试移除文件。"""
    try:
        return _read_checked_file(directory_fd, "credentials.json",
                                  _MAX_CREDENTIAL_BYTES)
    finally:
        try:
            os.unlink("credentials.json", dir_fd=directory_fd)
        except OSError:
            raise _RunnerError from None


def validate_environment(value: object) -> dict[str, str]:
    """校验脚本环境并返回副本，拒绝保留变量和大小写冲突。

    Raises:
        ValueError: 变量名称、值或体积不符合凭据传递约束。
    """

    if not isinstance(value, dict) or len(value) > _MAX_ENV_FIELDS:
        raise ValueError("runner environment is invalid")

    environment: dict[str, str] = {}
    folded_names: set[str] = set()
    total_bytes = 0
    try:
        for name, item in value.items():
            if not isinstance(name, str) or _ENV_NAME.fullmatch(name) is None:
                raise ValueError("runner environment is invalid")
            if not isinstance(item, str) or not item or "\x00" in item:
                raise ValueError("runner environment is invalid")
            folded = name.upper()
            if (folded in folded_names or folded in _RESERVED_ENV_NAMES or
                    folded.startswith(_RESERVED_ENV_PREFIXES)):
                raise ValueError("runner environment is invalid")
            name_bytes = name.encode("utf-8")
            value_bytes = item.encode("utf-8")
            if (len(name_bytes) > 128 or
                    len(value_bytes) > _MAX_ENV_FIELD_BYTES):
                raise ValueError("runner environment is invalid")
            total_bytes += len(value_bytes)
            if total_bytes > _MAX_ENV_TOTAL_BYTES:
                raise ValueError("runner environment is invalid")
            folded_names.add(folded)
            environment[name] = item
    except (UnicodeEncodeError, AttributeError):
        raise ValueError("runner environment is invalid") from None
    return environment


def _parse_environment(raw: bytes) -> dict[str, str]:
    credentials = _json_object(raw)
    if frozenset(credentials) != {"env"}:
        raise _RunnerError
    try:
        return validate_environment(credentials.get("env"))
    except ValueError:
        raise _RunnerError from None


def _verify_script(directory_fd: int, name: str, expected_digest: str) -> None:
    script_fd = _open_checked_file(directory_fd, name)
    digest = hashlib.sha256()
    total = 0
    try:
        script_stat = os.fstat(script_fd)
        if script_stat.st_size > _MAX_SCRIPT_BYTES:
            raise _RunnerError
        while total <= _MAX_SCRIPT_BYTES:
            chunk = os.read(script_fd,
                            min(64 * 1024, _MAX_SCRIPT_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        if total > _MAX_SCRIPT_BYTES or digest.hexdigest(
        ) != expected_digest.lower():
            raise _RunnerError
    except _RunnerError:
        raise
    except (OSError, OverflowError, ValueError):
        raise _RunnerError from None
    finally:
        try:
            os.close(script_fd)
        except OSError:
            pass


def _group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _signal_group(process_group: int, signal_number: int) -> None:
    try:
        os.killpg(process_group, signal_number)
    except ProcessLookupError:
        return
    except PermissionError:
        raise _RunnerError from None


def _reap_group(process_group: int) -> None:
    while True:
        try:
            child, _status = os.waitpid(-process_group, os.WNOHANG)
        except (ChildProcessError, ProcessLookupError):
            return
        except InterruptedError:
            continue
        if child == 0:
            return


def _install_sigterm_handler(cancelled: list[bool]) -> object | None:

    def handle_sigterm(_signal_number: int, _frame: object) -> None:
        cancelled[0] = True

    try:
        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, handle_sigterm)
        return previous
    except (OSError, ValueError):
        return None


def _parse_job(raw):
    if len(raw) >= _MAX_JOB_BYTES:
        raise _RunnerError
    job = _json_object(raw)
    if (len(raw) >= _MAX_JOB_BYTES or set(job) != {
            'protocol', 'runtime', 'sha256', 'dependencies', 'timeout_seconds',
            'install_timeout_seconds', 'script_name'
    } or type(job['protocol']) is not int or job['protocol'] != 1 or
            job['runtime'] not in ('python', 'node') or
            not isinstance(job['sha256'], str) or
            not re.fullmatch('[0-9a-f]{64}', job['sha256']) or
            job['script_name']
            not in (('script.py',) if job['runtime'] == 'python' else
                    ('script.js', 'script.mjs', 'script.cjs')) or
            any(
                type(job[key]) is not int or not 1 <= job[key] <= 240
                for key in ('timeout_seconds', 'install_timeout_seconds')) or
            not isinstance(job['dependencies'], list) or
            len(job['dependencies']) > 128):
        raise _RunnerError
    for dependency in job['dependencies']:
        if not isinstance(dependency, str) or not dependency or any(
                ord(c) < 32 or ord(c) > 126 for c in dependency):
            raise _RunnerError
        if job['runtime'] == 'python':
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._\-\[\],<>=!~+* ]*',
                                dependency):
                raise _RunnerError
        else:
            match = re.fullmatch(
                r'(@[a-z0-9._-]+/[a-z0-9._-]+|[a-z0-9._-]+)'
                r'(?:@([A-Za-z0-9._+*~^<>=| -]+))?', dependency)
            if not match or match[1].startswith(('.', '-')):
                raise _RunnerError
    if len(
            json.dumps(job['dependencies'],
                       ensure_ascii=False,
                       separators=(',', ':')).encode()) > 32768:
        raise _RunnerError
    return job


def _spawn(argv, **kwargs):
    return subprocess.Popen(argv, **kwargs)


def _drain(selector, collector, timeout):
    for key, _events in selector.select(max(0, timeout)):
        for _ in range(32):
            try:
                chunk = os.read(key.fileobj.fileno(), 8192)
            except BlockingIOError:
                break
            if not chunk:
                selector.unregister(key.fileobj)
                key.fileobj.close()
                break
            collector.add(key.data, chunk)


def _cleanup_process_group(process, selector, collector):
    """终止并回收整个进程组，清理后仍有存活进程则拒绝成功。"""
    for sig, grace in ((signal.SIGTERM, 0.25), (signal.SIGKILL, 1.0)):
        if _group_exists(process.pid):
            _signal_group(process.pid, sig)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            _drain(selector, collector, 0.01)
            if process.poll() is not None:
                _reap_group(process.pid)
            if not _group_exists(process.pid):
                break
    if process.poll() is None:
        process.kill()
        process.wait(timeout=1)
    _reap_group(process.pid)
    deadline = time.monotonic() + 0.1
    while selector.get_map() and time.monotonic() < deadline:
        _drain(selector, collector, 0.01)
    if _group_exists(process.pid):
        raise _RunnerError


def _process(argv, directory, environment, deadline, cancelled, collector):
    if time.monotonic() >= deadline:
        return 'timeout', None
    process = None
    selector = selectors.DefaultSelector()
    subreaper = _Subreaper()
    subreaper.enable()
    try:
        try:
            process = _spawn(argv,
                             cwd=str(directory),
                             env=environment,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                             shell=False,
                             start_new_session=True,
                             close_fds=True)
        except FileNotFoundError:
            raise _RuntimeUnavailable from None
        for pipe, name in ((process.stdout, 'stdout'), (process.stderr,
                                                        'stderr')):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        outcome = 'exited'
        exit_code = None
        while True:
            polled = process.poll()
            if polled is not None:
                exit_code = polled
                break
            if cancelled[0]:
                outcome = 'interrupted'
                break
            if time.monotonic() >= deadline:
                outcome = 'timeout'
                break
            _drain(selector, collector, min(0.05, deadline - time.monotonic()))
        # 主进程退出不代表后代已退出；正常完成也必须清理整个进程组。
        _cleanup_process_group(process, selector, collector)
        return outcome, exit_code
    finally:
        try:
            if process is not None and _group_exists(process.pid):
                _cleanup_process_group(process, selector, collector)
        finally:
            for key in list(selector.get_map().values()):
                key.fileobj.close()
            selector.close()
            subreaper.restore()


def _node_dependencies(dependencies):
    result = {}
    for item in dependencies:
        position = item.rfind('@')
        if position > 0:
            result[item[:position]] = item[position + 1:]
        else:
            result[item] = '*'
    return result


def run(directory: Path, stage='run'):
    """执行一个阶段，将运行及校验失败转换为固定协议结果。

    Args:
        directory: 保存任务、脚本及一次性凭据的私有目录。
        stage: install 仅安装依赖，run 消费凭据后执行脚本。

    Returns:
        包含进程状态、退出码、耗时及脱敏日志的结果字典。
    """
    started = time.monotonic()
    cancelled = [False]
    previous = _install_sigterm_handler(cancelled)
    directory_fd = None
    status, code, error = 'start_error', None, 'execution_unknown'
    collector = _LogCollector()
    try:
        if stage not in ('install', 'run'):
            raise _RunnerError
        directory = Path(os.path.abspath(directory))
        directory_fd = _check_directory(directory)
        # 安装器不能接触账号凭据，即使目录中已有凭据文件也拒绝执行。
        if stage == 'install':
            try:
                os.stat('credentials.json',
                        dir_fd=directory_fd,
                        follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise _RunnerError
        job = _parse_job(
            _read_checked_file(directory_fd, 'job.json', _MAX_JOB_BYTES))
        _verify_script(directory_fd, job['script_name'], job['sha256'])
        environment = {
            'PATH': '/usr/local/bin:/usr/bin:/bin',
            'LANG': 'C.UTF-8',
            'HOME': str(directory)
        }
        deadline = started + job['install_timeout_seconds' if stage ==
                                 'install' else 'timeout_seconds']
        if stage == 'run':
            # 先移除一次性文件，再将凭据仅注入脚本子进程环境。
            declared = _parse_environment(_consume_credentials(directory_fd))
            collector = _LogCollector(declared.values())
            environment.update(declared)
            executable = str(
                directory /
                '.venv/bin/python') if job['runtime'] == 'python' else 'node'
            commands = [[executable, str(directory / job['script_name'])]]
        elif job['runtime'] == 'python':
            status, code = _process(
                [sys.executable, '-m', 'venv',
                 str(directory / '.venv')], directory, environment, deadline,
                cancelled, collector)
            if status != 'exited' or code != 0:
                if status == 'exited':
                    raise _RuntimeUnavailable
                commands = []
            else:
                python = str(directory / '.venv/bin/python')
                commands = []
                if job['dependencies']:
                    commands.append([
                        python, '-m', 'pip', '--isolated', 'install',
                        '--disable-pip-version-check', '--no-input',
                        '--no-cache-dir', '--index-url',
                        'https://pypi.org/simple', *job['dependencies']
                    ])
                commands.append([python, '-m', 'pip', '--isolated', 'check'])
        else:
            if not shutil.which('node',
                                path=environment['PATH']) or not shutil.which(
                                    'npm', path=environment['PATH']):
                raise _RuntimeUnavailable
            package = directory / 'package.json'
            fd = os.open(
                package, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'w') as output:
                json.dump(
                    {
                        'private': True,
                        'dependencies': _node_dependencies(job['dependencies'])
                    }, output)
            commands = [[
                'npm', 'install', '--no-audit', '--no-fund',
                '--registry=https://registry.npmjs.org'
            ]]
        for command in commands:
            status, code = _process(command, directory, environment, deadline,
                                    cancelled, collector)
            if status != 'exited' or code != 0:
                break
        error = ('shutdown' if status == 'interrupted' else
                 ('install_timeout' if stage == 'install' else 'script_timeout')
                 if status == 'timeout' else
                 ('install_error' if stage == 'install' else 'script_error'
                 ) if code != 0 else None)
    except _RuntimeUnavailable:
        status, code, error = 'start_error', None, 'runtime_unavailable'
    except (OSError, ValueError, TypeError, OverflowError, RecursionError,
            _RunnerError, subprocess.SubprocessError, KeyboardInterrupt):
        status, code, error = 'interrupted' if cancelled[
            0] else 'start_error', None, 'shutdown' if cancelled[
                0] else 'execution_unknown'
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    log, truncated = collector.finish()
    return validate_result({
        'protocol': 1,
        'stage': stage,
        'process_status': status,
        'exit_code': code,
        'duration_ms': min(300000, int((time.monotonic() - started) * 1000)),
        'log': log,
        'log_truncated': truncated,
        'error_code': error
    })


if __name__ == '__main__':
    stage = sys.argv[1] if len(
        sys.argv) == 2 and sys.argv[1] in ('install', 'run') else 'run'
    result = run(Path(__file__).absolute().parent, stage)
    os.write(
        1,
        json.dumps(result, ensure_ascii=False,
                   separators=(',', ':')).encode('utf-8') + b'\n')
