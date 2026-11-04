"""验证不做迁移的单一状态快照边界。"""

import importlib
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if '_checkin_tests' not in sys.modules:
    package = types.ModuleType('_checkin_tests')
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
state_module = importlib.import_module('_checkin_tests.state')
MAX_ACTIVE_RUNS = state_module.MAX_ACTIVE_RUNS
InvalidStateError = state_module.InvalidStateError
capacity_bytes = state_module.capacity_bytes
finish_run = state_module.finish_run
new_snapshot = state_module.new_snapshot
validate_snapshot = state_module.validate_snapshot


def user():
    return {
        'platform_id': 'qq',
        'qq_id': '123',
        'origin': 'qq:FriendMessage:123',
        'cron': None,
        'auto_enabled': True,
        'schedule_watermark': 0.0,
        'last_schedule_error': None,
        'schedule_fingerprint': ''
    }


def run(index=0):
    return {
        'owner': '["qq","123"]',
        'script_id': f's{index}',
        'script_name': 'Script',
        'run_id': f'run-{index}',
        'source': 'manual',
        'manual_requested': True,
        'origin': 'qq:FriendMessage:123',
        'queued_at': 1.0,
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
        },
        'fingerprint': 'a' * 64,
        'binding_digest': 'b' * 64
    }


def populated():
    snapshot = new_snapshot()
    snapshot['users']['["qq","123"]'] = user()
    snapshot['runs']['["qq","123","s0"]'] = run()
    return snapshot


class StateTests(unittest.TestCase):

    def test_unknown_schema_is_not_migrated(self):
        state = new_snapshot()
        state['schema_version'] = 2
        with self.assertRaises(InvalidStateError):
            validate_snapshot(state)

    def test_capacity_reserves_actual_remaining_terminal_growth(self):
        state = populated()
        initial = capacity_bytes(state)
        state['runs']['["qq","123","s0"]']['log'] = '汉' * 1000
        self.assertEqual(capacity_bytes(state), initial)
        validate_snapshot(state, max_bytes=initial)
        with self.assertRaises(InvalidStateError):
            validate_snapshot(state, max_bytes=initial - 1)
        state['runs']['["qq","123","s0"]']['phase'] = 'completed'
        finish_run(state['runs']['["qq","123","s0"]'], now=2)
        self.assertLess(capacity_bytes(state), initial)

    def test_budget_handles_worst_escaped_safe_logs_and_resources(self):
        state = populated()
        record = state['runs']['["qq","123","s0"]']
        limit = capacity_bytes(state)
        record.update(log='\n' * 8192,
                      phase='cleaning',
                      cleanup_status='pending')
        state['cleanup_jobs']['run-0'] = {
            'owner': record['owner'],
            'script_id': 's0',
            'connection': 'a' * 64,
            'sandbox_id': 's' * 512,
            'cargo_id': 'c' * 512,
            'allocation_started': True
        }
        self.assertLessEqual(capacity_bytes(state), limit)
        validate_snapshot(state, max_bytes=limit)

    def test_active_count_is_bounded_without_eviction(self):
        state = populated()
        for index in range(1, MAX_ACTIVE_RUNS + 1):
            state['runs'][f'["qq","123","s{index}"]'] = run(index)
        with self.assertRaises(InvalidStateError):
            validate_snapshot(state)
        self.assertEqual(len(state['runs']), MAX_ACTIVE_RUNS + 1)

    def test_terminal_notification_rules(self):
        record = run()
        record.update(source='automatic', manual_requested=False, exit_code=0)
        finish_run(record, phase='completed', now=2)
        self.assertEqual(record['notification']['status'], 'none')
        record = run()
        record.update(source='automatic', manual_requested=False)
        finish_run(record, phase='failed', error_code='install_error', now=2)
        self.assertEqual(record['notification']['status'], 'pending')
        record = run()
        record.update(cleanup_status='pending')
        finish_run(record, phase='completed', now=2)
        self.assertEqual(record['notification']['status'], 'none')

    def test_owner_key_and_log_controls_rejected(self):
        state = populated()
        state['runs']['["qq","123","s0"]']['owner'] = '["qq","999"]'
        with self.assertRaises(InvalidStateError):
            validate_snapshot(state)
        state = populated()
        state['runs']['["qq","123","s0"]']['log'] = '\x00secret'
        with self.assertRaises(InvalidStateError):
            validate_snapshot(state)

    def test_terminal_growth_fits_exact_reserved_capacity_with_worst_values(
            self):
        state = populated()
        record = state['runs']['["qq","123","s0"]']
        bound = capacity_bytes(state)
        record.update(started_at=1234567890.1234567,
                      finished_at=1234567899.1234567,
                      phase='failed',
                      exit_code=-(2**31),
                      error_code='runtime_unavailable',
                      duration_ms=10**12,
                      sha256='f' * 64,
                      log='\\' * 8192,
                      log_truncated=True,
                      cleanup_status='pending',
                      stage='install',
                      process_status='interrupted')
        state['cleanup_jobs']['run-0'] = {
            'owner': record['owner'],
            'script_id': 's0',
            'connection': 'f' * 128,
            'sandbox_id': '\x01' * 512,
            'cargo_id': '\x01' * 512,
            'allocation_started': True
        }
        state['revision'] = 9999999999999999
        state['users'][
            record['owner']]['schedule_watermark'] = 1234567890.1234567
        state['users'][record['owner']]['schedule_fingerprint'] = 'f' * 64
        validate_snapshot(state, max_bytes=bound)

    def test_schedule_error_reserved_space_handles_control_escaped_origin(self):
        state = populated()
        user_record = state['users']['["qq","123"]']
        user_record['origin'] = '\x01' * 1024
        bound = capacity_bytes(state)
        user_record['last_schedule_error'] = {
            'code': 'capacity',
            'at': 1234567890.1234567,
            'origin': user_record['origin'],
            'notification': {
                'status': 'sending',
                'attempts': 3,
                'next_attempt_at': 1234567890.1234567
            }
        }
        validate_snapshot(state, max_bytes=bound)
