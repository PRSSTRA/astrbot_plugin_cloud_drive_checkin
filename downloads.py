"""公开 HTTPS 单文件下载；传输校验复用 dev@a662b766（项目许可证）。"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpcore
import httpx


class DownloadError(ValueError):
    """下载失败；错误不暴露 URL、响应或凭据。"""

    def __init__(self):
        self.code = 'download_error'
        super().__init__('download_error')


def _validate_url(url):
    try:
        if not isinstance(url, str) or any(
                ord(c) <= 32 or ord(c) == 127 for c in url):
            raise ValueError
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or
                parsed.port not in (None, 443) or parsed.username is not None or
                parsed.password is not None or '#' in url or '\\' in url or
                not parsed.path.startswith('/')):
            raise ValueError
        httpx.URL(url)
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and (not address.is_global or
                                    address.is_multicast or
                                    address.is_unspecified):
            raise ValueError
    except (ValueError, TypeError, httpx.InvalidURL):
        raise DownloadError() from None


async def _resolve_public_addresses(host, port):
    """拒绝任一非公网 DNS 答案，返回连接时直接使用的 IP 列表。"""
    if port != 443:
        raise DownloadError()
    try:
        answers = await asyncio.to_thread(socket.getaddrinfo, host, port,
                                          socket.AF_UNSPEC, socket.SOCK_STREAM)
        addresses = []
        for answer in answers:
            address = ipaddress.ip_address(answer[4][0])
            if (not address.is_global or address.is_multicast or
                    address.is_unspecified):
                raise DownloadError()
            if str(address) not in addresses:
                addresses.append(str(address))
        if not addresses:
            raise DownloadError()
        return tuple(addresses)
    except (OSError, RuntimeError, ValueError, TypeError, IndexError):
        raise DownloadError() from None


class _PublicIPNetworkBackend(httpcore.AsyncNetworkBackend):
    """仅向已校验的公网 IP 建立 TCP 连接，防止 DNS 重绑定。"""

    def __init__(self, backend=None):
        self._backend = httpcore.AnyIOBackend() if backend is None else backend

    async def connect_tcp(self,
                          host,
                          port,
                          timeout=None,
                          local_address=None,
                          socket_options=None):
        addresses = await _resolve_public_addresses(host, port)
        for address in addresses:
            try:
                return await self._backend.connect_tcp(
                    host=address,
                    port=port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout):
                pass
        raise httpcore.ConnectError('source unavailable')

    async def connect_unix_socket(self,
                                  path,
                                  timeout=None,
                                  socket_options=None):
        raise httpcore.ConnectError('source unavailable')

    async def sleep(self, seconds):
        await self._backend.sleep(seconds)


def _public_ip_transport():
    transport = httpx.AsyncHTTPTransport(trust_env=False,
                                         limits=httpx.Limits(
                                             max_connections=2,
                                             max_keepalive_connections=2))
    if not isinstance(transport._pool, httpcore.AsyncConnectionPool):
        raise DownloadError()
    # 只固定 TCP 目标，保留原始主机名用于 TLS SNI 和证书校验。
    transport._pool._network_backend = _PublicIPNetworkBackend()
    return transport


async def download_source(url: str) -> bytes:
    """限时下载非空源码，不跟随重定向、不使用代理，最多 1 MiB。

    Raises:
        DownloadError: 地址、DNS、传输、响应编码或大小不符合约束。
    """
    _validate_url(url)
    try:
        async with asyncio.timeout(60), httpx.AsyncClient(
                transport=_public_ip_transport(),
                follow_redirects=False,
                trust_env=False,
                timeout=60) as client:
            async with client.stream('GET',
                                     url,
                                     headers={'Accept-Encoding': 'identity'
                                             }) as response:
                if response.status_code != 200 or response.headers.get(
                        'content-encoding',
                        'identity').strip().lower() != 'identity':
                    raise DownloadError()
                declared = response.headers.get('content-length')
                if declared is not None and not 0 <= int(
                        declared) <= 1024 * 1024:
                    raise DownloadError()
                data = bytearray()
                async for chunk in response.aiter_raw():
                    if len(data) + len(chunk) > 1024 * 1024:
                        raise DownloadError()
                    data.extend(chunk)
                if not data:
                    raise DownloadError()
                return bytes(data)
    except (httpx.HTTPError, OSError, ValueError, TimeoutError):
        raise DownloadError() from None
