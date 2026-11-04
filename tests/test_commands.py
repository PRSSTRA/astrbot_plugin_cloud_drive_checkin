"""使用虚构账号验证命令分发与原生会话等待契约。"""

import asyncio
import importlib
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType('_checkin_commands')
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault(pkg.__name__, pkg)


def setUpModule():
    global _root, _environment
    _root = tempfile.TemporaryDirectory(prefix='checkin-command-tests-')
    _environment = patch.dict(os.environ, {
        'ASTRBOT_ROOT': _root.name,
        'ASTRBOT_CLI': '1'
    })
    _environment.start()


def tearDownModule():
    _environment.stop()
    _root.cleanup()


class CommandContract(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.main = importlib.import_module('_checkin_commands.main')
        self.plugin = object.__new__(self.main.CloudDriveCheckin)
        self.plugin._terminating = False
        self.plugin._sessions = {}
        self.plugin._entry_lock = asyncio.Lock()
        self.plugin.config = {'enabled': True, 'whitelist_qqs': ['10001']}
        self.plugin._send = AsyncMock()
        self.plugin._dispatch = AsyncMock()

    async def test_invalid_arguments_only_help_and_no_input_echo(self):
        for text in ('云盘签到', '云盘签到 非法秘密', '云盘签到 运行 多余秘密'):
            event = types.SimpleNamespace(get_message_str=lambda: text)
            self.assertIsNone(self.plugin._operation(event))
        self.assertEqual(
            self.plugin._operation(
                types.SimpleNamespace(get_message_str=lambda: '云盘签到 运行')), '运行')

    async def test_menu_snapshot_uses_stable_identifier(self):
        script = types.SimpleNamespace(id='first', name='第一个')
        self.plugin._ask = AsyncMock(return_value=('1', None))
        selected = await self.plugin._choose(None, None, '运行', [script])
        self.assertIs(selected, script)

    async def test_replacement_waits_for_old_waiter_cleanup(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def old():
            try:
                entered.set()
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        task = asyncio.create_task(old())
        await entered.wait()
        self.plugin._sessions['owner'] = task
        await self.plugin._cancel_session('owner')
        self.assertTrue(cleaned.is_set())
        self.assertNotIn('owner', self.plugin._sessions)

    async def test_native_reload_can_retry_unconfirmed_shutdown(self):
        self.plugin._shutdown_task = None
        self.plugin._shutdown = AsyncMock(
            side_effect=[OSError('pending write'), None])
        with self.assertRaises(OSError):
            await self.plugin.terminate()
        await self.plugin.terminate()
        self.assertEqual(self.plugin._shutdown.await_count, 2)

    async def test_read_access_uses_confirmed_snapshot_during_inflight_commit(
            self):
        event = self.event('云盘签到 列表')
        event.is_admin = lambda: False
        self.plugin.engine = object()
        self.plugin.state_store = types.SimpleNamespace(
            ready=False, snapshot=lambda: {'revision': 1})
        self.assertEqual(self.plugin._access(event).qq_id, '10001')

    async def test_native_waiter_keeps_answer_arriving_during_prompt_send(self):
        from astrbot.core.utils.session_waiter import FILTERS
        from astrbot.core.utils.session_waiter import SessionWaiter
        from astrbot.core.utils.session_waiter import USER_SESSIONS
        identity = self.main.get_private_identity(self.event('original'))
        original = self.event('original')
        incoming = self.event('  secret#value  ')

        async def send(_event, _text):
            self.assertTrue(USER_SESSIONS)
            session_id = next(
                f.filter(incoming)
                for f in FILTERS
                if f.filter(incoming) in USER_SESSIONS)
            await SessionWaiter.trigger(session_id, incoming)
            await asyncio.sleep(0)

        self.plugin._send = send
        self.plugin._access = lambda event: identity
        text, received = await self.plugin._ask(original, identity, '设置',
                                                'prompt')
        self.assertEqual(text, '  secret#value  ')
        self.assertIs(received, incoming)
        self.assertFalse(USER_SESSIONS)
        self.assertTrue(incoming.stopped)
        self.assertFalse(incoming.llm)

    async def test_native_waiter_unregisters_when_prompt_send_fails(self):
        from astrbot.core.utils.session_waiter import USER_SESSIONS
        original = self.event('original')
        identity = self.main.get_private_identity(original)
        self.plugin._send = AsyncMock(
            side_effect=OSError('fake transport failure'))
        with self.assertRaises(OSError):
            await self.plugin._ask(original, identity, '设置', 'prompt')
        self.assertFalse(USER_SESSIONS)

    @staticmethod
    def event(text):

        class Event:

            def __init__(self):
                self.message_obj = types.SimpleNamespace(
                    raw_message={
                        'post_type': 'message',
                        'message_type': 'private',
                        'sub_type': 'friend',
                        'user_id': '10001',
                        'sender': {
                            'user_id': '10001'
                        },
                        'message': [{
                            'type': 'text',
                            'data': {
                                'text': text
                            }
                        }]
                    })
                self.unified_msg_origin = 'qq:FriendMessage:10001'
                self.is_at_or_wake_command = False
                self.stopped, self.llm = False, True

            def get_platform_name(self):
                return 'aiocqhttp'

            def get_platform_id(self):
                return 'qq'

            def get_sender_id(self):
                return '10001'

            def should_call_llm(self, value):
                self.llm = value

            def stop_event(self):
                self.stopped = True

            def get_message_str(self):
                return text

        return Event()

    async def test_log_blocks_preserve_order_and_limit(self):
        blocks = self.main.log_blocks('甲' * 3201, True)
        self.assertEqual(len(blocks), 3)
        self.assertTrue(all(len(block) <= 1500 for block in blocks))
        self.assertIn('1/3', blocks[0])
        self.assertIn('截断', blocks[-1])


if __name__ == '__main__':
    unittest.main()
