"""验证源码下载的公网地址约束与响应边界。"""

import socket
import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx

from downloads import _PublicIPNetworkBackend
from downloads import _resolve_public_addresses
from downloads import download_source
from downloads import DownloadError


class BytesStream(httpx.AsyncByteStream):

    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


class DownloadTests(unittest.IsolatedAsyncioTestCase):

    async def test_rejects_unsafe_urls_without_network(self):
        for url in ('http://example.com/a.py', 'https://127.0.0.1/a',
                    'https://u:p@example.com/a', 'https://example.com:444/a',
                    'https://example.com/a#x', 'https://example.com/\na'):
            with self.subTest(url=url), self.assertRaises(DownloadError):
                await download_source(url)

    async def test_resolves_all_addresses_and_rejects_mixed_private_answer(
            self):
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))
        ]
        with patch('socket.getaddrinfo',
                   return_value=answers), self.assertRaises(DownloadError):
            await _resolve_public_addresses('example.com', 443)

    async def test_connects_only_verified_numeric_address(self):
        backend = AsyncMock()
        with patch('downloads._resolve_public_addresses',
                   return_value=('8.8.8.8',)):
            await _PublicIPNetworkBackend(backend).connect_tcp(
                'example.com', 443)
        self.assertEqual(backend.connect_tcp.call_args.kwargs['host'],
                         '8.8.8.8')

    async def test_download_refreshes_and_rejects_redirects_or_encoding(self):
        bodies = iter((b'first', b'second'))
        transport = httpx.MockTransport(
            lambda r: httpx.Response(200, stream=BytesStream(next(bodies))))
        with patch('downloads._public_ip_transport', return_value=transport):
            self.assertEqual(
                await download_source('https://example.com/a?branch=main'),
                b'first')
            self.assertEqual(
                await download_source('https://example.com/a?branch=main'),
                b'second')
        for response in (httpx.Response(
                302, headers={'location': 'https://example.com/b'}),
                         httpx.Response(200, stream=BytesStream(b'')),
                         httpx.Response(200,
                                        headers={'content-encoding': 'gzip'},
                                        stream=BytesStream(b'bad')),
                         httpx.Response(200,
                                        stream=BytesStream(b'x' *
                                                           (1024 * 1024 + 1)))):
            with patch('downloads._public_ip_transport',
                       return_value=httpx.MockTransport(lambda r: response)):
                with self.assertRaises(DownloadError):
                    await download_source('https://example.com/a')
