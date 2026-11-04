"""验证独立 runner 的安装、凭据消费与受控进程结果。"""

import hashlib
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from runners import bootstrap


class RunnerTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'checkin'
        self.directory.mkdir(mode=0o700)

    def prepare(self,
                source=b'import os; print(os.environ["TOKEN"])',
                runtime='python',
                dependencies=()):
        name = 'script.py' if runtime == 'python' else 'script.js'
        self.write(name, source)
        self.write(
            'job.json',
            json.dumps({
                'protocol': 1,
                'runtime': runtime,
                'sha256': hashlib.sha256(source).hexdigest(),
                'dependencies': list(dependencies),
                'timeout_seconds': 2,
                'install_timeout_seconds': 60,
                'script_name': name
            }).encode())

    def write(self, name, data):
        path = self.directory / name
        path.write_bytes(data)
        path.chmod(0o600)

    def credentials(self, env=None):
        self.write(
            'credentials.json',
            json.dumps({
                'env': env or {
                    'TOKEN': 'never-print-me'
                }
            }).encode())

    def test_install_rejects_credentials_until_run(self):
        self.prepare()
        self.credentials()
        result = bootstrap.run(self.directory, 'install')
        self.assertEqual(result['process_status'], 'start_error')
        (self.directory / 'credentials.json').unlink()
        result = bootstrap.run(self.directory, 'install')
        self.assertEqual(result['exit_code'], 0, result)
        self.credentials()
        result = bootstrap.run(self.directory, 'run')
        self.assertEqual(result['exit_code'], 0, result)
        self.assertNotIn('never-print-me', result['log'])
        self.assertIn('[REDACTED]', result['log'])
        self.assertFalse((self.directory / 'credentials.json').exists())

    def test_stream_redaction_control_sequences_chunks_and_bounded_tail(self):
        secret = 'abc\x1b[31mdef\nGHI'
        collector = bootstrap._LogCollector([secret])
        for chunk in (b'noise ' * 3000, b'abc\x1b[3', b'1mdef\nG',
                      b'HI token=other-secret\n', b'end'):
            collector.add('stdout', chunk)
        log, truncated = collector.finish()
        self.assertNotIn('abcdef\nGHI', log)
        self.assertNotIn('other-secret', log)
        self.assertIn('[REDACTED]', log)
        self.assertTrue(truncated)
        self.assertLessEqual(len(log.encode()), 4096)

    def test_timeout_and_stderr_are_safe_process_results(self):
        self.prepare(
            b'import time,sys; print("password=hidden",file=sys.stderr,flush=True); time.sleep(5)'
        )
        self.assertEqual(
            bootstrap.run(self.directory, 'install')['exit_code'], 0)
        self.credentials()
        result = bootstrap.run(self.directory, 'run')
        self.assertEqual(result['process_status'], 'timeout')
        self.assertEqual(result['error_code'], 'script_timeout')
        self.assertNotIn('hidden', result['log'])
        self.assertIn('[stderr]', result['log'])

    def test_private_files_hash_and_result_protocol_are_strict(self):
        self.prepare()
        (self.directory / 'script.py').chmod(0o644)
        self.assertEqual(
            bootstrap.run(self.directory, 'install')['process_status'],
            'start_error')
        with self.assertRaises(ValueError):
            bootstrap.validate_environment({'TOKEN': ''})
        with self.assertRaises(ValueError):
            bootstrap.validate_environment({'A' * 129: 'x'})
        with self.assertRaises(ValueError):
            bootstrap.validate_result({'protocol': 1})

    def test_real_pip_installs_generated_wheel_offline_and_script_imports_it(
            self):
        wheel_dir = Path(self.temp.name) / 'wheels'
        wheel_dir.mkdir()
        wheel = wheel_dir / 'checkin_fixture-1.0-py3-none-any.whl'
        with zipfile.ZipFile(wheel, 'w') as archive:
            archive.writestr('checkin_fixture.py',
                             'VALUE = "wheel-installed"\n')
            archive.writestr(
                'checkin_fixture-1.0.dist-info/METADATA',
                'Metadata-Version: 2.1\nName: checkin-fixture\nVersion: 1.0\n')
            archive.writestr(
                'checkin_fixture-1.0.dist-info/WHEEL',
                'Wheel-Version: 1.0\nGenerator: checkin-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n'
            )
            archive.writestr('checkin_fixture-1.0.dist-info/RECORD', '')
        self.prepare(b'import checkin_fixture; print(checkin_fixture.VALUE)',
                     dependencies=['checkin-fixture==1.0'])
        spawn = bootstrap._spawn
        commands = []

        def controlled(argv, **kwargs):
            commands.append(argv[:])
            if 'pip' in argv and 'install' in argv:
                index = argv.index('--index-url')
                argv = argv[:index] + [
                    '--no-index', '--find-links',
                    str(wheel_dir)
                ] + argv[index + 2:]
            return spawn(argv, **kwargs)

        with patch.object(bootstrap, '_spawn', side_effect=controlled):
            result = bootstrap.run(self.directory, 'install')
        self.assertEqual(result['exit_code'], 0, result)
        self.assertTrue(
            any('install' in argv and 'pip' in argv for argv in commands))
        self.credentials()
        result = bootstrap.run(self.directory, 'run')
        self.assertEqual(result['exit_code'], 0, result)
        self.assertIn('wheel-installed', result['log'])

    @unittest.skipUnless(
        shutil.which('node') and shutil.which('npm'), 'node/npm unavailable')
    def test_real_npm_installs_generated_package_from_controlled_registry(self):
        package = io.BytesIO()
        with tarfile.open(fileobj=package, mode='w:gz') as archive:
            for name, content in {
                    'package/package.json':
                        b'{"name":"checkin-fixture","version":"1.0.0","main":"index.js"}',
                    'package/index.js':
                        b'module.exports = "npm-installed"'
            }.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        payload = package.getvalue()

        class Registry(BaseHTTPRequestHandler):

            def do_GET(self):
                if self.path.endswith('.tgz'):
                    body = payload
                else:
                    body = json.dumps({
                        'name': 'checkin-fixture',
                        'dist-tags': {
                            'latest': '1.0.0'
                        },
                        'versions': {
                            '1.0.0': {
                                'name': 'checkin-fixture',
                                'version': '1.0.0',
                                'dist': {
                                    'tarball':
                                        f'http://127.0.0.1:'
                                        f'{self.server.server_port}/fixture.tgz',
                                    'shasum': hashlib.sha1(payload).hexdigest()
                                }
                            }
                        }
                    }).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Registry)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.prepare(b'console.log(require("checkin-fixture"))', 'node',
                     ['checkin-fixture@1.0.0'])
        spawn = bootstrap._spawn
        commands = []
        host_node, host_npm = shutil.which('node'), shutil.which('npm')
        which = shutil.which

        def controlled_which(name, **kwargs):
            return {
                'node': host_node,
                'npm': host_npm
            }.get(name) or which(name, **kwargs)

        def controlled(argv, **kwargs):
            commands.append(argv[:])
            argv = [
                f'--registry=http://127.0.0.1:{server.server_port}'
                if arg.startswith('--registry=') else arg for arg in argv
            ]
            if argv[0] in ('node', 'npm'):
                argv = [host_node if argv[0] == 'node' else host_npm, *argv[1:]]
            kwargs['env'] = {
                **kwargs['env'], 'PATH':
                    str(Path(host_node).parent) + ':' + kwargs['env']['PATH']
            }
            return spawn(argv, **kwargs)

        with patch.object(bootstrap, '_spawn',
                          side_effect=controlled), patch.object(
                              bootstrap.shutil,
                              'which',
                              side_effect=controlled_which):
            result = bootstrap.run(self.directory, 'install')
        self.assertEqual(result['exit_code'], 0, result)
        self.assertTrue(
            any('npm' in argv[0] and 'install' in argv for argv in commands))
        self.credentials()
        with patch.object(bootstrap, '_spawn', side_effect=controlled):
            result = bootstrap.run(self.directory, 'run')
        self.assertEqual(result['exit_code'], 0, result)
        self.assertIn('npm-installed', result['log'])
