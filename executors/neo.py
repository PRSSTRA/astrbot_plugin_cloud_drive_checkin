"""串行执行 Neo 两阶段任务，并持久化资源清理状态。

Ownership cleanup adapted from dev a662b766 (repository license).
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from pathlib import Path
import re
import shlex
import time
from urllib.parse import urlsplit

try:
    from ..runners.bootstrap import validate_environment
    from ..runners.bootstrap import validate_result
    from ..state import finish_run
except ImportError:
    from runners.bootstrap import validate_environment, validate_result
    from state import finish_run

from .client import NeoClient
from .client import NeoError

_SAFE_ERRORS = {
    'config_changed', 'permission_revoked', 'shutdown', 'execution_unknown',
    'runtime_unavailable', 'install_error', 'install_timeout', 'script_error',
    'script_timeout'
}


class ExecutionBlocked(RuntimeError):
    """用固定错误码中止执行，避免暴露底层异常。"""

    def __init__(self, code='execution_unknown'):
        self.code = code if code in _SAFE_ERRORS else 'execution_unknown'
        super().__init__(self.code)


def _authorize(guard, snapshot):
    result = guard(snapshot)
    if result is not None:
        if inspect.iscoroutine(result):
            result.close()
        raise ExecutionBlocked()


def _command(program):
    return 'python3 -c ' + shlex.quote(program)


def _private_check_program(hashes):
    return '''import os,stat,hashlib
p=os.lstat('checkin')
assert stat.S_ISDIR(p.st_mode) and p.st_uid==os.geteuid() and stat.S_IMODE(p.st_mode)==0o700
fd=os.open('checkin',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
q=os.fstat(fd)
assert (p.st_dev,p.st_ino)==(q.st_dev,q.st_ino)
assert not os.path.lexists('checkin/credentials.json')
for name,digest in ''' + repr(hashes) + '''.items():
 f=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
 s=os.fstat(f)
 assert stat.S_ISREG(s.st_mode) and s.st_uid==os.geteuid() and stat.S_IMODE(s.st_mode)==0o600 and s.st_nlink==1
 assert s.st_size<=1048576
 with os.fdopen(f,'rb') as stream: assert hashlib.sha256(stream.read(1048577)).hexdigest()==digest
os.close(fd)
print('verified')'''


class NeoExecutor:
    """串行分配沙箱、安装依赖、注入凭据并确认清理。"""

    def __init__(self, store, client: NeoClient):
        self.store, self.client = store, client
        self._serial = asyncio.Lock()
        self._accepting = True
        self._recovered = False
        self._active = set()
        self._cleanup = set()
        self._close_task = None

    @property
    def quiescent(self):
        """是否已停止接单，且没有执行或清理任务。"""
        return not self._accepting and not self._active and not self._cleanup

    @property
    def blocked(self):
        """是否因停机、恢复、存储或待清理资源而禁止新执行。"""
        return (not self._accepting or not self._recovered or
                not self.store.ready or
                bool(self.store.snapshot()['cleanup_jobs']))

    def _current(self, snapshot, key, run_id):
        run = snapshot['runs'].get(key)
        if run is None or run['run_id'] != run_id:
            raise ExecutionBlocked()
        return run

    def _guard(self, guard):
        if not self._accepting:
            raise ExecutionBlocked('shutdown')
        if not self.store.ready:
            raise ExecutionBlocked()
        _authorize(guard, self.store.snapshot())

    async def _change(self, key, run_id, change, guard=None):

        def mutate(snapshot):
            run = self._current(snapshot, key, run_id)
            if guard is not None:
                _authorize(guard, snapshot)
            change(snapshot, run)

        await self.store.mutate(mutate)

    async def _shell(self, sandbox_id, program, *, expected=None):
        response = await self.client.exec(sandbox_id, _command(program), 30)
        if not response['success'] or response['exit_code'] != 0 or (
                expected is not None and
                response['output'].strip() != expected):
            raise ExecutionBlocked()

    @staticmethod
    def _owned(cargo, sandbox_id, cargo_id):
        return (type(cargo) is dict and cargo.get('id') == cargo_id and
                cargo.get('managed') is True and
                cargo.get('managed_by_sandbox_id') == sandbox_id)

    async def _clean_resources(self, sandbox_id, cargo_id):
        """核对资源关联与 cargo 归属后清理，不一致时保留资源。"""
        if not sandbox_id or not cargo_id:
            return False
        try:
            sandbox = await self.client.get_sandbox(sandbox_id)
            cargo = await self.client.get_cargo(cargo_id)
            if sandbox is not None and (sandbox.get('id') != sandbox_id or
                                        sandbox.get('cargo_id') != cargo_id):
                return False
            if cargo is not None and not self._owned(cargo, sandbox_id,
                                                     cargo_id):
                return False
            await self.client.delete_sandbox(sandbox_id)
            if await self.client.get_sandbox(sandbox_id) is not None:
                return False
            cargo = await self.client.get_cargo(cargo_id)
            if cargo is not None:
                if not self._owned(cargo, sandbox_id, cargo_id):
                    return False
                await self.client.delete_cargo(cargo_id)
            return await self.client.get_cargo(cargo_id) is None
        except NeoError:
            return False

    async def _finalize(self, key, run_id, result, job):
        if await self.store.wait_ready():

            def record_result(snapshot, run):
                run.update(result)
                run['cleanup_status'] = 'pending'

            await self._change(key, run_id, record_result)
        cleaned = not job['allocation_started'] or await self._clean_resources(
            job['sandbox_id'], job['cargo_id'])
        if not await self.store.wait_ready():
            return

        def finish(snapshot, run):
            run.update(result)
            run['cleanup_status'] = 'cleaned' if cleaned else 'pending'
            if not cleaned and run.get('error_code') is None:
                run['error_code'] = 'cleanup_pending'
            if cleaned:
                snapshot['cleanup_jobs'].pop(run_id, None)
            finish_run(run)

        await self._change(key, run_id, finish)

    async def recover(self):
        """核验连接及任务归属，清理遗留资源后才允许接单。"""
        async with self._serial:
            if not self._accepting or not self.store.ready:
                raise ExecutionBlocked()
            self._recovered = False
            await self.client.check_access()
            snapshot = self.store.snapshot()
            for run_id, job in snapshot['cleanup_jobs'].items():
                key = next((key for key, run in snapshot['runs'].items()
                            if run['run_id'] == run_id), None)
                if (key is None or job['connection']
                        != self.client.connection.fingerprint or
                        any(value is not None and
                            not re.fullmatch('[A-Za-z0-9_-]{1,128}', value)
                            for value in (job['sandbox_id'], job['cargo_id']))):
                    raise ExecutionBlocked()
                run = snapshot['runs'][key]
                if job['owner'] != run['owner'] or job['script_id'] != run[
                        'script_id']:
                    raise ExecutionBlocked()
                result = {} if run['phase'] in (
                    'completed', 'failed', 'timed_out', 'interrupted',
                    'cancelled') else {
                        'phase': 'interrupted',
                        'error_code': 'shutdown',
                        'process_status': 'interrupted',
                        'exit_code': None
                    }
                await self._finalize(key, run_id, result, job)
            self._recovered = True

    async def execute(self, run, script, source, *, guard, credentials,
                      timeout_seconds, install_timeout_seconds):
        """串行执行任务，返回清理状态已记录的运行快照。

        Args:
            guard: 同步授权回调，拒绝时抛错，允许时返回 None。
            credentials: 安装成功且重新校验文件后才调用的凭据提供器。
            timeout_seconds: 脚本阶段时限，单位为秒。
            install_timeout_seconds: 安装阶段时限，单位为秒。

        Raises:
            ExecutionBlocked: 执行前的状态、授权或输入校验未通过。
        """
        task = asyncio.current_task()
        self._active.add(task)
        try:
            async with self._serial:
                if self.blocked:
                    raise ExecutionBlocked()
                key = next(
                    (key for key, item in self.store.snapshot()['runs'].items()
                     if item['run_id'] == run['run_id']), None)
                if key is None:
                    raise ExecutionBlocked()
                run_id = run['run_id']
                self._guard(guard)
                if (type(source) is not bytes or
                        not 0 < len(source) <= 1048576 or any(
                            type(v) is not int or not 1 <= v <= 240
                            for v in (timeout_seconds,
                                      install_timeout_seconds))):
                    raise ExecutionBlocked()
                suffix = Path(urlsplit(script.url).path).suffix.lower()
                name = ('script.py'
                        if script.runtime == 'python' else 'script' +
                        (suffix if suffix in ('.mjs', '.cjs') else '.js'))
                source_hash = hashlib.sha256(source).hexdigest()
                job_bytes = json.dumps(
                    {
                        'protocol': 1,
                        'runtime': script.runtime,
                        'sha256': source_hash,
                        'dependencies': list(script.dependencies),
                        'timeout_seconds': timeout_seconds,
                        'install_timeout_seconds': install_timeout_seconds,
                        'script_name': name
                    },
                    ensure_ascii=False,
                    separators=(',', ':')).encode('utf-8')
                if len(job_bytes) >= 65536:
                    raise ExecutionBlocked('config_changed')
                runner = Path(__file__).parent.parent.joinpath(
                    'runners/bootstrap.py').read_bytes()
                payloads = {
                    'bootstrap.py': runner,
                    'job.json': job_bytes,
                    name: source
                }
                hashes = {
                    path: hashlib.sha256(data).hexdigest()
                    for path, data in payloads.items()
                }
                job = {
                    'owner': run['owner'],
                    'script_id': run['script_id'],
                    'connection': self.client.connection.fingerprint,
                    'sandbox_id': None,
                    'cargo_id': None,
                    'allocation_started': True
                }
                result = {
                    'phase': 'failed',
                    'error_code': 'execution_unknown',
                    'exit_code': None,
                    'duration_ms': 0,
                    'log': '',
                    'log_truncated': False
                }
                install_result = None
                allocated = False
                try:
                    await self.client.check_access()
                    self._guard(guard)

                    def creating(snapshot, current):
                        current.update(phase='creating',
                                       sha256=source_hash,
                                       cleanup_status='pending')
                        snapshot['cleanup_jobs'][run_id] = dict(job)

                    await self._change(key, run_id, creating, guard)
                    allocated = True
                    try:
                        self._guard(guard)
                    except Exception:
                        job['allocation_started'] = False
                        raise
                    resource = await self.client.create_sandbox(run_id)
                    job.update(sandbox_id=resource['id'],
                               cargo_id=resource['cargo_id'])

                    def record(snapshot, current):
                        snapshot['cleanup_jobs'][run_id] = dict(job)

                    await self._change(key, run_id, record)
                    # 创建回执不足以证明归属，上传前还需分别核对沙箱和 cargo。
                    sandbox = await self.client.get_sandbox(job['sandbox_id'])
                    cargo = await self.client.get_cargo(job['cargo_id'])
                    if (sandbox is None or
                            sandbox.get('cargo_id') != job['cargo_id'] or
                            sandbox.get('profile')
                            != self.client.connection.profile or
                            not {'shell', 'filesystem'}.issubset(
                                sandbox.get('capabilities', [])) or
                            not self._owned(cargo, job['sandbox_id'],
                                            job['cargo_id'])):
                        raise ExecutionBlocked()
                    program = ('import os\nos.umask(0o077)\n'
                               'os.mkdir("checkin",0o700)\n')
                    for path in payloads:
                        program += ('fd=os.open(' + repr('checkin/' + path) +
                                    ',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)\n'
                                    'os.close(fd)\n')
                    program += 'print("ready")'
                    await self._shell(job['sandbox_id'],
                                      program,
                                      expected='ready')
                    for path, data in payloads.items():
                        await self.client.upload(job['sandbox_id'],
                                                 'checkin/' + path, data)
                    # 安装阶段不上传凭据，避免依赖安装脚本接触账号信息。
                    await self._change(
                        key, run_id, lambda snapshot, current: current.update(
                            phase='installing'), guard)
                    response = await self.client.exec(
                        job['sandbox_id'],
                        'python3 checkin/bootstrap.py install',
                        install_timeout_seconds + 10)
                    if response['exit_code'] != 0 or not response['success']:
                        raise ExecutionBlocked()
                    install_result = validate_result(
                        json.loads(response['output']), 'install')
                    result.update(log=install_result['log'],
                                  log_truncated=install_result['log_truncated'],
                                  duration_ms=install_result['duration_ms'])
                    if install_result[
                            'process_status'] != 'exited' or install_result[
                                'exit_code'] != 0:
                        result.update(
                            phase='timed_out'
                            if install_result['process_status'] == 'timeout'
                            else 'failed',
                            error_code=install_result['error_code'] or
                            'install_error',
                            exit_code=install_result['exit_code'],
                            stage='install',
                            process_status=install_result['process_status'])
                    else:
                        self._guard(guard)
                        # 安装可能改写共享目录；注入凭据前重新验证权限及文件摘要。
                        await self._shell(job['sandbox_id'],
                                          _private_check_program(hashes),
                                          expected='verified')
                        await self.client.upload(job['sandbox_id'],
                                                 'checkin/bootstrap.py', runner)
                        self._guard(guard)
                        environment = validate_environment(credentials())
                        credential_bytes = json.dumps(
                            {
                                'env': environment
                            },
                            ensure_ascii=False,
                            separators=(',', ':')).encode('utf-8')
                        if len(credential_bytes) > 512 * 1024:
                            raise ExecutionBlocked()
                        await self._shell(job['sandbox_id'],
                                          '''import os
fd=os.open('checkin/credentials.json',os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
os.close(fd)
print('ready')''',
                                          expected='ready')
                        self._guard(guard)
                        await self.client.upload(job['sandbox_id'],
                                                 'checkin/credentials.json',
                                                 credential_bytes)

                        def running(snapshot, current):
                            current.update(phase='running',
                                           started_at=time.time())

                        await self._change(key, run_id, running, guard)
                        self._guard(guard)
                        response = await self.client.exec(
                            job['sandbox_id'],
                            'python3 checkin/bootstrap.py run',
                            timeout_seconds + 10)
                        if (response['exit_code'] != 0 or
                                not response['success']):
                            raise ExecutionBlocked()
                        executed = validate_result(
                            json.loads(response['output']), 'run')
                        result.update(
                            phase='timed_out'
                            if executed['process_status'] == 'timeout' else
                            'interrupted' if executed['process_status']
                            == 'interrupted' else 'completed'
                            if executed['process_status'] == 'exited' and
                            executed['exit_code'] == 0 else 'failed',
                            exit_code=executed['exit_code'],
                            error_code=executed['error_code'],
                            stage='run',
                            process_status=executed['process_status'],
                            duration_ms=install_result['duration_ms'] +
                            executed['duration_ms'],
                            log=install_result['log'] + executed['log'],
                            log_truncated=install_result['log_truncated'] or
                            executed['log_truncated'])
                except asyncio.CancelledError:
                    result.update(phase='interrupted',
                                  error_code='shutdown',
                                  process_status='interrupted')
                except Exception as exc:
                    code = getattr(exc, 'code', None)
                    code = code if code in _SAFE_ERRORS else 'execution_unknown'
                    result.update(
                        phase='cancelled' if code in ('config_changed',
                                                      'permission_revoked',
                                                      'shutdown') else 'failed',
                        error_code=code)
                finally:
                    if allocated:
                        # 调用方取消不能中断清理；未确认删除的资源继续阻断后续执行。
                        cleanup = asyncio.create_task(
                            self._finalize(key, run_id, result, job))
                        self._cleanup.add(cleanup)
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            await asyncio.shield(cleanup)
                        finally:
                            self._cleanup.discard(cleanup)
                    elif self.store.ready:
                        await self._change(
                            key, run_id, lambda snapshot, current: finish_run(
                                current,
                                phase=result['phase'],
                                error_code=result['error_code']))
                return self._current(self.store.snapshot(), key, run_id)
        finally:
            self._active.discard(task)

    async def close(self):
        """停止接单，取消执行并等待清理，再关闭客户端。"""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self):
        self._accepting = False
        tasks = tuple(self._active)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._cleanup:
            await asyncio.gather(*tuple(self._cleanup), return_exceptions=True)
        await self.client.close()
