"""Isolated AstrBot 4.29.3 + native KV + loopback OneBot/FakeBay smoke.

Uses only self-authored programs and fake credentials. No production Neo/LLM/cloud.
"""
import argparse
import asyncio
import base64
import contextlib
import importlib.metadata
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import time

PLUGIN = 'astrbot_plugin_cloud_drive_checkin'
CONTROL = 'CHECKIN_NATIVE '
CANARY = 'FAKE_NATIVE_SECRET#WITH_SLASH\\END'


def emit(kind, **values):
    print(CONTROL + json.dumps({
        'kind': kind,
        **values
    }, ensure_ascii=False),
          flush=True)


def fixture():
    return {
        'enabled':
            True,
        'whitelist_qqs': ['10001', '10002'],
        'cron':
            '0 7 * * *',
        'scripts': [{
            '__template_key': 'script',
            'id': 'native_python',
            'name': 'Native Python',
            'enabled': True,
            'url': 'https://example.com/latest.py',
            'runtime': 'python',
            'env_names': ['TOKEN'],
            'dependencies': []
        }, {
            '__template_key': 'script',
            'id': 'native_node',
            'name': 'Native Node',
            'enabled': True,
            'url': 'https://example.com/latest.mjs',
            'runtime': 'node',
            'env_names': [],
            'dependencies': []
        }]
    }


def isolate(root):
    """隔离框架目录、代理环境与子进程的非回环套接字访问。"""
    os.environ['ASTRBOT_ROOT'] = str(root)
    os.environ['ASTRBOT_CLI'] = '1'
    sys.path.insert(0, str(root))
    for key in tuple(os.environ):
        if key.lower().endswith('_proxy'):
            del os.environ[key]

    # 在加载 AstrBot 前安装钩子，限制框架及插件发起的套接字访问。
    def audit(event, args):
        if event in {'socket.connect', 'socket.bind'} and isinstance(
                args[1], tuple):
            host = args[1][0]
            try:
                allowed = ipaddress.ip_address(host).is_loopback
            except ValueError:
                allowed = host == 'localhost'
            if not allowed:
                raise PermissionError(
                    'native smoke permits loopback sockets only')

    sys.addaudithook(audit)


async def child(args):
    """在隔离目录启动原生框架，处理父进程的验收控制请求。"""
    isolate(args.root)
    from astrbot.core import astrbot_config
    from astrbot.core import db_helper
    from astrbot.core import LogBroker
    from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
    astrbot_config['dashboard'].update(enable=False,
                                       host='127.0.0.1',
                                       port=args.dashboard_port)
    astrbot_config['admins_id'] = ['90001']
    astrbot_config['provider'] = []
    astrbot_config['provider_sources'] = []
    astrbot_config['wake_prefix'] = ['/']
    astrbot_config['provider_settings']['enable'] = False
    astrbot_config['platform_settings']['enable_id_white_list'] = False
    astrbot_config['platform'] = [{
        'id': f'native-bot-{index}',
        'type': 'aiocqhttp',
        'enable': True,
        'ws_reverse_host': '127.0.0.1',
        'ws_reverse_port': port,
        'ws_reverse_token': ''
    } for index, port in enumerate(args.ports)]
    astrbot_config.save_config()
    core = AstrBotCoreLifecycle(LogBroker(), db_helper)
    await core.initialize()

    def current():
        metadata = core.star_context.get_registered_star(PLUGIN)
        if metadata is None or metadata.star_cls is None:
            raise RuntimeError('native loader did not load plugin')
        return metadata.star_cls

    source_version = 1
    source_calls = []

    def configure_download():

        async def download(url):
            source_calls.append(url)
            if url.endswith('.mjs'):
                return b'console.log("native-node-ok"); process.exitCode=0;'
            return (f'import os,sys\nprint("native-source-{source_version}")\n'
                    'print(os.environ["TOKEN"])\nsys.exit(7)\n').encode()

        current().engine._download = download

    configure_download()
    running = asyncio.create_task(core.start())
    try:
        emit('ready',
             loaded=True,
             version=importlib.metadata.version('AstrBot'))
        while line := await asyncio.to_thread(sys.stdin.readline):
            request = json.loads(line)
            op = request['op']
            plugin = current()
            if op == 'stop':
                break
            if op == 'observe':
                from astrbot.core.utils.session_waiter import USER_SESSIONS
                emit('reply',
                     state=plugin.state_store.snapshot(),
                     waiters=len(USER_SESSIONS),
                     source_calls=len(source_calls),
                     ready=plugin.state_store.ready,
                     pending=plugin.state_store.has_pending)
            elif op == 'source':
                source_version += 1
                emit('reply')
            elif op == 'save':
                from astrbot.dashboard.services.config_service import \
                    ConfigFileService
                merged = dict(plugin.config)
                merged.update(request['config'])
                await ConfigFileService(core).save_plugin_configs(
                    merged, PLUGIN)
                configure_download()
                emit('reply', loaded=current().engine is not None)
            elif op == 'schedule':
                await plugin.engine._schedule_once(now=request['now'])
                emit('reply')
            else:
                raise ValueError('unknown smoke operation')
    finally:
        await core.stop()
        await running


class Process:
    """管理隔离 AstrBot 子进程及其独立控制消息通道。"""

    def __init__(self, root, endpoint, ports, env, dashboard_port):
        self.root = root
        self.endpoint = endpoint
        self.ports = ports
        self.env = env
        self.dashboard_port = dashboard_port
        self.events, self.logs = asyncio.Queue(), []
        self.process = None

    async def start(self):
        """启动子进程，分离日志与控制消息并等待框架就绪。"""
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).resolve()),
            '--child',
            '--root',
            str(self.root),
            '--bay',
            self.endpoint,
            '--ports',
            *map(str, self.ports),
            '--dashboard-port',
            str(self.dashboard_port),
            cwd=self.root,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)

        async def reader():
            async for line in self.process.stdout:
                text = line.decode(errors='replace').rstrip()
                if text.startswith(CONTROL):
                    await self.events.put(json.loads(text[len(CONTROL):]))
                else:
                    self.logs.append(text)
            await self.events.put({'kind': 'exit'})

        self.reader = asyncio.create_task(reader())
        reply = await self.event()
        assert reply.get('kind') == 'ready', '\n'.join(self.logs)
        return self

    async def event(self):
        """读取下一条控制事件，超时时附带已收集的框架日志。"""
        try:
            return await asyncio.wait_for(self.events.get(), 90)
        except TimeoutError:
            raise AssertionError('\n'.join(self.logs)) from None

    async def request(self, op, **values):
        """发送一条控制请求并等待对应回复。"""
        self.process.stdin.write((json.dumps({
            'op': op,
            **values
        }) + '\n').encode())
        await self.process.stdin.drain()
        reply = await self.event()
        assert reply.get('kind') == 'reply', (reply, '\n'.join(self.logs))
        return reply

    async def close(self):
        """请求正常退出，超时后强制终止并检查退出结果。"""
        if self.process is None:
            return
        if self.process.returncode is None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.process.stdin.write(b'{"op":"stop"}\n')
                await self.process.stdin.drain()
            try:
                await asyncio.wait_for(self.process.wait(), 30)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        await self.reader
        assert self.process.returncode == 0, '\n'.join(self.logs)


class Peer:
    """通过回环 WebSocket 模拟一个 OneBot 私聊实例。"""

    def __init__(self, port, index):
        self.port, self.index = port, index
        self.inbox, self.messages = asyncio.Queue(), []
        self.sequence = 0

    async def start(self):
        """连接反向 WebSocket，并处理验收所需的 OneBot 动作。"""
        from websockets.asyncio.client import connect
        async with asyncio.timeout(20):
            while True:
                try:
                    self.ws = await connect(
                        f'ws://127.0.0.1:{self.port}/ws',
                        additional_headers={
                            'X-Self-ID': str(12345 + self.index),
                            'X-Client-Role': 'Universal'
                        },
                        proxy=None)
                    break
                except OSError:
                    await asyncio.sleep(.05)

        async def reader():
            async for raw in self.ws:
                request = json.loads(raw)
                action, params = request['action'], request.get('params', {})
                if action in {'send_msg', 'send_private_msg', 'send_group_msg'}:
                    message = params.get('message', '')
                    text = message if isinstance(message, str) else ''.join(
                        p.get('data', {}).get('text', '') for p in message)
                    self.messages.append((params, text))
                    await self.inbox.put((params, text))
                    data = {'message_id': 20000 + len(self.messages)}
                elif action == 'get_login_info':
                    data = {
                        'user_id': 12345 + self.index,
                        'nickname': 'NativeBot'
                    }
                elif action == 'get_stranger_info':
                    data = {
                        'user_id': params['user_id'],
                        'nickname': 'NativeUser'
                    }
                elif action in {'get_group_list', 'get_friend_list'}:
                    data = []
                else:
                    raise AssertionError(action)
                await self.ws.send(
                    json.dumps({
                        'status': 'ok',
                        'retcode': 0,
                        'data': data,
                        'echo': request.get('echo')
                    }))

        self.reader = asyncio.create_task(reader())
        return self

    async def send(self, text, qq=10001):
        """发送指定虚构用户的私聊消息事件。"""
        self.sequence += 1
        await self.ws.send(
            json.dumps({
                'time': int(time.time()),
                'self_id': 12345 + self.index,
                'post_type': 'message',
                'message_type': 'private',
                'sub_type': 'friend',
                'message_id': self.sequence,
                'user_id': qq,
                'sender': {
                    'user_id': qq,
                    'nickname': 'NativeUser'
                },
                'message': [{
                    'type': 'text',
                    'data': {
                        'text': text
                    }
                }],
                'raw_message': text,
                'font': 0
            }))

    async def reply(self, qq=10001):
        """读取下一条私聊回复，并检查目标用户。"""
        params, text = await asyncio.wait_for(self.inbox.get(), 30)
        assert int(params['user_id']) == qq, (params, text)
        return text

    async def command(self, text, qq=10001):
        """发送命令并读取下一条私聊回复。"""
        await self.send(text, qq)
        return await self.reply(qq)

    async def close(self):
        """关闭 WebSocket 并等待消息读取任务退出。"""
        await self.ws.close()
        await self.reader


def port():
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        return server.getsockname()[1]


async def wait_state(process, predicate):
    """轮询已确认的状态快照，直到满足指定验收条件。"""
    async with asyncio.timeout(90):
        while True:
            response = await process.request('observe')
            if predicate(response['state']):
                return response
            await asyncio.sleep(.05)


async def acceptance(source):
    """用 FakeBay、虚构凭据与回环 OneBot 验证原生加载全链路。"""
    assert importlib.metadata.version('AstrBot') == '4.29.3'
    from aiohttp import web
    import httpx
    sys.path.insert(0, str(source / 'tests'))
    from neo_support import FakeBay
    bay = FakeBay()

    async def handle(request):
        response = await bay(
            httpx.Request(request.method,
                          'http://127.0.0.1' + request.path_qs,
                          headers=request.headers,
                          content=await request.read()))
        return web.Response(status=response.status_code,
                            body=await response.aread(),
                            headers={
                                'Content-Type':
                                    response.headers.get(
                                        'content-type', 'application/json')
                            })

    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.router.add_route('*', '/{path:.*}', handle)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, '127.0.0.1', 0)
    # FakeBay 只在回环地址提供受控服务，不连接真实 Neo。
    await site.start()
    endpoint = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
    process, peers = None, []
    try:
        with tempfile.TemporaryDirectory(prefix='checkin-native-') as temporary:
            root = Path(temporary)
            plugin_dir = root / 'data' / 'plugins' / PLUGIN
            plugin_dir.mkdir(parents=True)
            for name in ('main.py', 'config.py', 'permissions.py',
                         'credentials.py', 'state.py', 'storage.py',
                         'engine.py', 'downloads.py', '_conf_schema.json',
                         'metadata.yaml', 'requirements.txt'):
                shutil.copyfile(source / name, plugin_dir / name)
            for name in ('runners', 'executors'):
                shutil.copytree(source / name,
                                plugin_dir / name,
                                ignore=shutil.ignore_patterns('__pycache__'))
            config_dir = root / 'data' / 'config'
            config_dir.mkdir(parents=True)
            config = fixture()
            config['neo_connection'] = {
                'enabled': True,
                'docker_cleanup_confirmed': True,
                'endpoint': endpoint,
                'access_key_env': 'CLOUD_DRIVE_CHECKIN_NEO_KEY',
                'ttl': 900
            }
            (config_dir / f'{PLUGIN}_config.json').write_text(
                json.dumps(config), encoding='utf-8')
            # 显式构造子进程环境，不继承宿主凭据或代理配置。
            env = {
                'PATH':
                    os.environ.get('PATH', os.defpath),
                'HOME':
                    str(root),
                'LANG':
                    'C.UTF-8',
                'CLOUD_DRIVE_CHECKIN_MASTER_KEY':
                    base64.urlsafe_b64encode(os.urandom(32)).decode(),
                'CLOUD_DRIVE_CHECKIN_NEO_KEY':
                    bay.token,
                'PYTHONUNBUFFERED':
                    '1'
            }
            ports = [port(), port()]
            process = await Process(root, endpoint, ports, env, port()).start()
            peers = [
                await Peer(p, index).start() for index, p in enumerate(ports)
            ]
            first, second = peers
            for command in ('/云盘签到', '/云盘签到 非法秘密', '/云盘签到 运行 多余秘密'):
                reply = await first.command(command)
                assert ('云盘签到 设置' in reply and '非法秘密' not in reply and
                        '多余秘密' not in reply), reply
                assert (await process.request('observe'))['waiters'] == 0
            assert 'Native Python' in await first.command('/云盘签到 列表')
            await first.command('/云盘签到 设置')
            await first.command('1')
            await first.command('1')
            assert '已加密保存' in await first.command(
                CANARY.replace('\\', '\\\\').replace('#', '\\#'))
            before = (await process.request('observe'))['state']
            assert CANARY not in json.dumps(before, ensure_ascii=False)
            isolated = await second.command('/云盘签到 列表')
            assert '未运行' in isolated, (isolated, await
                                       process.request('observe'),
                                       '\n'.join(process.logs[-12:]))
            await first.command('/云盘签到 设置')
            assert '云盘签到 日志' in await first.command('/云盘签到')
            assert (await process.request('observe'))['waiters'] == 0
            await first.command('/云盘签到 运行')
            assert '已确认' in await first.command('1')
            result = await wait_state(
                process, lambda s: bool(s['runs']) and all(r['notification'][
                    'status'] == 'submitted' for r in s['runs'].values()))
            run = next(iter(result['state']['runs'].values()))
            assert run['exit_code'] == 7 and run['phase'] == 'failed', run
            assert CANARY not in run['log'] and 'native-source-1' in run[
                'log'], run
            await first.reply()  # 消费终态通知，避免影响下一条命令回复。
            await first.command('/云盘签到 日志')
            assert 'native-source-1' in await first.command('1')
            await process.request('source')
            await first.command('/云盘签到 运行')
            await first.command('1')
            result = await wait_state(
                process, lambda s: next(iter(s['runs'].values()))['run_id'] !=
                run['run_id'] and next(iter(s['runs'].values()))[
                    'notification']['status'] == 'submitted')
            assert 'native-source-2' in next(
                iter(result['state']['runs'].values()))['log']
            await first.reply()
            await process.request(
                'save',
                config={'scripts': list(reversed(fixture()['scripts']))})
            assert 'Native Node' in await first.command('/云盘签到 列表')
            assert (await process.request('observe')
                   )['state']['bindings'] == before['bindings']
            await first.command('/云盘签到 设置')
            await first.command('1')
            assert '已加密保存' in await first.command('1')  # 排序后首项为零字段 Node。
            await first.command('/云盘签到 运行')
            await first.command('1')
            result = await wait_state(
                process, lambda s: len(s['runs']) == 2 and any(r[
                    'script_id'] == 'native_node' and r['notification'][
                        'status'] == 'submitted' for r in s['runs'].values()))
            node = next(r for r in result['state']['runs'].values()
                        if r['script_id'] == 'native_node')
            assert node['exit_code'] == 0 and 'native-node-ok' in node[
                'log'], node
            await first.reply()
            await first.command('/云盘签到 设置')
            await first.command('2')
            assert '个人定时已保存' in await first.command('* * * * *')
            result = await wait_state(
                process, lambda s: len(s['runs']) == 2 and all(
                    r['source'] == 'automatic' and r['phase'] not in {
                        'queued', 'downloading', 'creating', 'installing',
                        'running', 'cleaning'
                    } for r in s['runs'].values()) and
                any(r['script_id'] == 'native_python' and r['notification'][
                    'status'] == 'submitted' for r in s['runs'].values()))
            automatic_node = next(r for r in result['state']['runs'].values()
                                  if r['script_id'] == 'native_node')
            assert automatic_node['notification']['status'] == 'none'
            assert '错误=script_error' in await first.reply()
            await first.command('/云盘签到 设置')
            await first.command('3')
            assert '自动开关已保存' in await first.command('关')
            persisted = (await process.request('observe'))['state']
            for peer in peers:
                await peer.close()
            peers = []
            await process.close()
            process = await Process(root, endpoint, ports, env, port()).start()
            peers = [
                await Peer(p, index).start() for index, p in enumerate(ports)
            ]
            first, second = peers
            restarted = await process.request('observe')
            assert restarted['source_calls'] == 0
            assert restarted['state']['bindings'] == persisted['bindings']
            assert {
                k: r['run_id'] for k, r in restarted['state']['runs'].items()
            } == {
                k: r['run_id'] for k, r in persisted['runs'].items()
            }
            await first.command('/云盘签到 设置')
            await first.command('3')
            assert '自动执行已关闭' in await first.command('关')
            await first.command('/云盘签到 设置')
            _params, timeout_notice = await asyncio.wait_for(
                first.inbox.get(), 75)
            assert '本次设置已超时结束' in timeout_notice
            assert (await process.request('observe'))['waiters'] == 0
            await process.request('save', config={'scripts': []})
            await first.command('/云盘签到 详情')
            assert '退出码：7' in await first.command('1')
            await first.command('/云盘签到 日志')
            assert 'native-source-2' in await first.command('1')
            await first.command('/云盘签到 设置')
            await first.command('4')
            await first.command('1')
            assert '已删除' in await first.command('确认')
            remaining = (await process.request('observe'))['state']
            assert len(remaining['bindings']) == 1 and len(
                remaining['runs']) == 1
            assert not result['state'][
                'cleanup_jobs'] and not bay.sandboxes and not bay.cargos
            assert CANARY not in json.dumps([peer.messages for peer in peers],
                                            ensure_ascii=False)
            assert (await process.request('observe'))['waiters'] == 0
            print(
                json.dumps(
                    {
                        'framework':
                            '4.29.3',
                        'native_loader_KV':
                            True,
                        'onebot_instances':
                            2,
                        'latest_source_updated':
                            True,
                        'latest_records':
                            len(result['state']['runs']),
                        'python_exit':
                            7,
                        'node_exit':
                            0,
                        'native_save_hot_reload':
                            True,
                        'outgoing_secret_absent':
                            True,
                        'residual_resources':
                            0,
                        'actual_cron_trigger':
                            True,
                        'automatic_exit0_silent':
                            True,
                        'automatic_failure_notice':
                            True,
                        'cold_restart_no_replay':
                            True,
                        'native_60s_timeout':
                            True,
                        'removed_script_owner_query_delete':
                            True,
                        'neo':
                            'FakeBay; not isolation proof',
                        'ui':
                            'not part of CLI smoke; verify separately in a browser'
                    },
                    ensure_ascii=False))
    finally:
        for peer in peers:
            await peer.close()
        if process:
            await process.close()
        await server.cleanup()
        await bay.close()


def main():
    """解析命令行参数并运行父进程验收或隔离子进程。"""
    if not __debug__:
        raise RuntimeError('smoke requires assertions')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir',
                        type=Path,
                        default=Path(__file__).resolve().parents[1])
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--root', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--bay', help=argparse.SUPPRESS)
    parser.add_argument('--ports', nargs=2, type=int, help=argparse.SUPPRESS)
    parser.add_argument('--dashboard-port', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    asyncio.run(
        child(args) if args.child else acceptance(args.source_dir.resolve()))


if __name__ == '__main__':
    main()
