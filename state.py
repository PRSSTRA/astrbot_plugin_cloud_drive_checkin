"""单版本、有容量上限的状态快照，改写自本项目预发布开发版。"""
from __future__ import annotations

import json
import math
import re
import time

STATE_KEY = 'checkin_state'
SCHEMA_VERSION = 1
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_ACTIVE_RUNS = 64
RUN_RESERVE_BYTES = 32 * 1024
ACTIVE_PHASES = frozenset(
    {'queued', 'downloading', 'creating', 'installing', 'running', 'cleaning'})
TERMINAL_PHASES = frozenset(
    {'completed', 'failed', 'timed_out', 'interrupted', 'cancelled'})
ERROR_CODES = frozenset({
    'download_error', 'runtime_unavailable', 'install_error', 'install_timeout',
    'script_error', 'script_timeout', 'execution_unknown', 'cleanup_pending',
    'config_changed', 'permission_revoked', 'shutdown'
})
NOTIFICATION_STATUSES = frozenset(
    {'none', 'pending', 'sending', 'submitted', 'failed', 'unknown'})
_GROWTH_BASE = {
    'started_at': None,
    'finished_at': None,
    'phase': 'queued',
    'exit_code': None,
    'error_code': None,
    'duration_ms': 0,
    'sha256': None,
    'log': '',
    'log_truncated': False,
    'cleanup_status': 'none',
    'notification': {
        'status': 'none',
        'attempts': 0,
        'next_attempt_at': 0.0
    }
}


class InvalidStateError(ValueError):
    """快照结构、关联关系或预留容量不符合约束。"""


def new_snapshot():
    """创建空快照，不迁移或丢弃未知版本数据。"""
    return {
        'schema_version': SCHEMA_VERSION,
        'revision': 0,
        'users': {},
        'bindings': {},
        'runs': {},
        'cleanup_jobs': {}
    }


def serialized_bytes(value):
    """按持久化 JSON 格式计算 UTF-8 字节数。"""
    return len(
        json.dumps(value,
                   ensure_ascii=False,
                   separators=(',', ':'),
                   allow_nan=False).encode('utf-8'))


def unsettled(run):
    """任务、资源清理或通知仍未结算时返回 True。"""
    return run['phase'] in ACTIVE_PHASES or run[
        'cleanup_status'] == 'pending' or run['notification']['status'] in {
            'pending', 'sending'
        }


def notification_required(run):
    """手动参与必通知；纯自动正常退出和配置取消保持静默。"""
    if run['manual_requested']:
        return True
    if run['phase'] == 'cancelled' and run['error_code'] in {
            'config_changed', 'permission_revoked', 'shutdown'
    }:
        return False
    return run['phase'] != 'completed' or run['exit_code'] != 0 or run[
        'error_code'] is not None


def finish_run(run, *, phase=None, error_code=None, now=None):
    """就地结束记录并安排必要通知，保留已确认的进程结果。"""
    if phase is not None and run['phase'] not in {
            'completed', 'failed', 'timed_out'
    }:
        run['phase'] = phase
    if error_code is not None and run.get('error_code') is None:
        run['error_code'] = error_code
    if run['phase'] not in TERMINAL_PHASES:
        run['phase'] = 'interrupted'
    if run.get('finished_at') is None:
        run['finished_at'] = time.time() if now is None else now
    if run['cleanup_status'] != 'pending' and run['notification'][
            'status'] == 'none' and notification_required(run):
        run['notification'] = {
            'status': 'pending',
            'attempts': 0,
            'next_attempt_at': 0.0
        }
    return run


def capacity_bytes(snapshot):
    """计算快照占用及活动任务、通知、调度错误所需的收尾预留。"""
    total = serialized_bytes(snapshot) + max(
        0, 16 - len(str(snapshot['revision'])))
    for run in snapshot['runs'].values():
        if run['phase'] in ACTIVE_PHASES or run['cleanup_status'] == 'pending':
            growth = sum(
                max(
                    0,
                    serialized_bytes(run.get(key, value)) -
                    serialized_bytes(value))
                for key, value in _GROWTH_BASE.items())
            growth += sum(
                serialized_bytes(run[key]) + len(key) + 4
                for key in ('stage', 'process_status')
                if key in run)
            job = snapshot['cleanup_jobs'].get(run['run_id'])
            if job is not None:
                growth += serialized_bytes({run['run_id']: job}) - 2
            total += max(0, RUN_RESERVE_BYTES - growth)
        elif run['notification']['status'] in {'pending', 'sending'}:
            total += max(0, 256 - serialized_bytes(run['notification']))
    # 即使达到容量上限，每个用户仍可持久化一次有界调度错误。
    for user in snapshot['users'].values():
        total += max(0,
                     8192 - (serialized_bytes(user['last_schedule_error']) - 4))
        total += max(0, 32 - serialized_bytes(user['schedule_watermark']))
        total += max(
            0, 66 - serialized_bytes(user.get('schedule_fingerprint', '')))
        if 'schedule_fingerprint' not in user:
            total += len('schedule_fingerprint') + 4
    return total


def _require(condition):
    if not condition:
        raise InvalidStateError('invalid checkin_state snapshot')


def _text(value, limit, *, empty=False):
    _require(
        isinstance(value, str) and (empty or bool(value)) and
        len(value.encode('utf-8')) <= limit and '\x00' not in value)


def _number(value, *, nullable=False):
    if nullable and value is None:
        return
    _require(
        type(value) in (int, float) and math.isfinite(value) and
        0 <= value <= 10**16)


def _notification(value):
    _require(
        isinstance(value, dict) and
        set(value) == {'status', 'attempts', 'next_attempt_at'})
    _require(value['status'] in NOTIFICATION_STATUSES and
             type(value['attempts']) is int and 0 <= value['attempts'] <= 3)
    _number(value['next_attempt_at'])


def _key(key, expected):
    try:
        parsed = json.loads(key)
    except (TypeError, ValueError):
        raise InvalidStateError('invalid identity key') from None
    _require(parsed == expected)


def validate_snapshot(snapshot, *, max_bytes=MAX_SNAPSHOT_BYTES):
    """校验完整快照并原样返回；不修复、迁移或清空无效数据。

    Raises:
        InvalidStateError: 结构、版本、用户关联或含预留的容量无效。
    """
    try:
        _validate(snapshot, max_bytes)
    except (KeyError, TypeError, UnicodeError, OverflowError,
            ValueError) as exc:
        if isinstance(exc, InvalidStateError):
            raise
        raise InvalidStateError('invalid checkin_state snapshot') from None
    return snapshot


def _validate(snapshot, max_bytes):
    _require(
        isinstance(snapshot, dict) and set(snapshot) == {
            'schema_version', 'revision', 'users', 'bindings', 'runs',
            'cleanup_jobs'
        })
    _require(
        type(snapshot['schema_version']) is int and
        snapshot['schema_version'] == SCHEMA_VERSION)
    _require(
        type(snapshot['revision']) is int and
        0 <= snapshot['revision'] < 10**16)
    for name in ('users', 'bindings', 'runs', 'cleanup_jobs'):
        _require(isinstance(snapshot[name], dict))
    for owner, user in snapshot['users'].items():
        _require(
            set(user) in ({
                'platform_id', 'qq_id', 'origin', 'cron', 'auto_enabled',
                'schedule_watermark', 'last_schedule_error'
            }, {
                'platform_id', 'qq_id', 'origin', 'cron', 'auto_enabled',
                'schedule_watermark', 'last_schedule_error',
                'schedule_fingerprint'
            }))
        for field, limit in (('platform_id', 128), ('qq_id', 32), ('origin',
                                                                   1024)):
            _text(user[field], limit)
        _key(owner, [user['platform_id'], user['qq_id']])
        _require(type(user['auto_enabled']) is bool)
        if user['cron'] is not None:
            _text(user['cron'], 256)
        _text(user.get('schedule_fingerprint', ''), 64, empty=True)
        _number(user['schedule_watermark'])
        error = user['last_schedule_error']
        if error is not None:
            _require(set(error) == {'code', 'at', 'origin', 'notification'})
            _require(error['code'] == 'capacity')
            _number(error['at'])
            _text(error['origin'], 1024)
            _notification(error['notification'])
            _require(serialized_bytes(error) <= 8192)
    for key, binding in snapshot['bindings'].items():
        _require(
            set(binding) ==
            {'owner', 'script_id', 'script_name', 'fields_hash', 'ciphertext'})
        _validate_identity_record(key, binding, snapshot)
        _text(binding['ciphertext'], 1024 * 1024)
        _hash(binding['fields_hash'])
    run_ids = set()
    active = 0
    for key, run in snapshot['runs'].items():
        required = {
            'owner', 'script_id', 'script_name', 'run_id', 'source',
            'manual_requested', 'origin', 'queued_at', 'started_at',
            'finished_at', 'phase', 'exit_code', 'error_code', 'duration_ms',
            'sha256', 'log', 'log_truncated', 'cleanup_status', 'notification',
            'fingerprint', 'binding_digest'
        }
        _require(required <= set(run) <= required | {'stage', 'process_status'})
        _validate_identity_record(key, run, snapshot)
        _text(run['run_id'], 64)
        _require(run['run_id'] not in run_ids)
        run_ids.add(run['run_id'])
        _require(run['source'] in {'manual', 'automatic'} and
                 type(run['manual_requested']) is bool)
        _text(run['origin'], 1024)
        for field in ('queued_at', 'started_at', 'finished_at'):
            _number(run[field], nullable=field != 'queued_at')
        _require(run['phase'] in ACTIVE_PHASES | TERMINAL_PHASES)
        active += run['phase'] in ACTIVE_PHASES
        _require(run['exit_code'] is None or
                 (type(run['exit_code']) is int and
                  -(2**31) <= run['exit_code'] < 2**31))
        _require(run['error_code'] is None or run['error_code'] in ERROR_CODES)
        _require(
            type(run['duration_ms']) is int and
            0 <= run['duration_ms'] <= 10**12)
        if run['sha256'] is not None:
            _hash(run['sha256'])
        _text(run['log'], 8192, empty=True)
        _require(not any(ord(c) < 32 and c not in '\n\t' for c in run['log']))
        _require(
            type(run['log_truncated']) is bool and
            run['cleanup_status'] in {'none', 'pending', 'cleaned'})
        _notification(run['notification'])
        _hash(run['fingerprint'])
        _hash(run['binding_digest'])
        if 'stage' in run:
            _require(run['stage'] in {'install', 'run'})
        if 'process_status' in run:
            _require(run['process_status'] in
                     {'exited', 'timeout', 'start_error', 'interrupted'})
    _require(active <= MAX_ACTIVE_RUNS)
    for run_id, job in snapshot['cleanup_jobs'].items():
        _require(
            set(job) == {
                'owner', 'script_id', 'connection', 'sandbox_id', 'cargo_id',
                'allocation_started'
            })
        _text(run_id, 64)
        _require(job['owner'] in snapshot['users'])
        _text(job['script_id'], 64)
        _text(job['connection'], 128)
        for field in ('sandbox_id', 'cargo_id'):
            if job[field] is not None:
                _text(job[field], 512)
        _require(type(job['allocation_started']) is bool)
    _require(capacity_bytes(snapshot) <= max_bytes)


def _hash(value):
    _require(
        isinstance(value, str) and
        re.fullmatch('[a-f0-9]{64}', value) is not None)


def _validate_identity_record(key, record, snapshot):
    _require(record['owner'] in snapshot['users'])
    user = snapshot['users'][record['owner']]
    _require(
        re.fullmatch('[a-z][a-z0-9_]{0,63}', record['script_id']) is not None)
    _text(record['script_name'], 256)
    _key(key, [user['platform_id'], user['qq_id'], record['script_id']])
