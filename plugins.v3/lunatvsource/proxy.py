"""Small stdlib-only HTTP/SOCKS5 proxy adapter for plugin-owned requests."""
from __future__ import annotations

import base64
import http.client
import re
import socket
import ssl
import urllib.parse
from dataclasses import dataclass
from typing import Optional, Tuple


class ProxyError(ConnectionError):
    """The configured upstream proxy could not serve a request."""


class ProxyConnectionError(ProxyError):
    """The client could not establish or keep an upstream proxy connection."""


class ProxyTargetTimeout(ProxyError):
    """The target server did not respond before the proxy request timeout."""


class ProxyTargetTLSError(ProxyError):
    """TLS negotiation with the target server failed over the proxy."""


class ProxyResponseError(ProxyError):
    """The proxy returned an HTTP error response."""

    def __init__(self, status_code: int, message: Optional[str] = None) -> None:
        self.status_code = int(status_code)
        super().__init__(message or f"代理返回错误（HTTP {self.status_code}）")


class ProxyAuthenticationError(ProxyResponseError):
    """The proxy rejected the configured credentials."""

    def __init__(self, status_code: int = 407) -> None:
        super().__init__(status_code, f"代理认证失败（HTTP {int(status_code)}）")


@dataclass(frozen=True)
class ProxySpec:
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def redacted(self) -> str:
        auth = "<redacted>@" if self.username or self.password else ""
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{self.scheme}://{auth}{host}:{self.port}"


def parse_proxy_url(value: object) -> Optional[ProxySpec]:
    if isinstance(value, ProxySpec):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "socks5", "socks5h"}:
            raise ValueError("代理协议必须是 http 或 socks5")
        if not parsed.hostname:
            raise ValueError("代理地址缺少主机名")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("代理地址不能包含路径、查询参数或片段")
        port = parsed.port if parsed.port is not None else 7890
    except ValueError as exc:
        message = str(exc)
        if message in {
            "代理协议必须是 http 或 socks5",
            "代理地址缺少主机名",
            "代理地址不能包含路径、查询参数或片段",
        }:
            raise
        raise ValueError("代理端口无效") from exc
    if not 1 <= port <= 65535:
        raise ValueError("代理端口无效")
    return ProxySpec(
        scheme="socks5" if parsed.scheme.lower() == "socks5h" else parsed.scheme.lower(),
        host=parsed.hostname,
        port=port,
        username=urllib.parse.unquote(parsed.username or ""),
        password=urllib.parse.unquote(parsed.password or ""),
    )


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise OSError("代理连接提前关闭")
        chunks.extend(chunk)
    return bytes(chunks)


def classify_proxy_exception(exc: BaseException) -> ProxyError:
    """Map transport and CONNECT failures to safe, actionable proxy errors."""
    if isinstance(exc, ProxyError):
        return exc
    tunnel_match = re.search(r"Tunnel connection failed:\s*(\d{3})", str(exc))
    if tunnel_match:
        status_code = int(tunnel_match.group(1))
        if status_code == 407:
            return ProxyAuthenticationError(status_code)
        return ProxyResponseError(status_code)
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return ProxyTargetTimeout("目标站点超时")
    if isinstance(exc, ssl.SSLError):
        return ProxyTargetTLSError("目标站点 TLS 握手失败")
    return ProxyConnectionError("代理连接失败")


def _socks5_socket(spec: ProxySpec, target_host: str, target_port: int, timeout: float) -> socket.socket:
    if not target_host or not 1 <= int(target_port) <= 65535:
        raise ValueError("SOCKS5 目标地址无效")
    sock = socket.create_connection((spec.host, spec.port), timeout)
    try:
        methods = [0]
        if spec.username or spec.password:
            methods.append(2)
        sock.sendall(bytes([5, len(methods), *methods]))
        version, method = _recv_exact(sock, 2)
        if version != 5 or method == 255 or method not in {0, 2}:
            raise OSError("SOCKS5 代理不接受认证方式")
        if method == 2:
            if not (spec.username or spec.password):
                raise OSError("SOCKS5 代理要求认证")
            user = spec.username.encode("utf-8")
            password = spec.password.encode("utf-8")
            if len(user) > 255 or len(password) > 255:
                raise OSError("SOCKS5 认证信息过长")
            sock.sendall(bytes([1, len(user)]) + user + bytes([len(password)]) + password)
            if _recv_exact(sock, 2) != bytes([1, 0]):
                raise OSError("SOCKS5 代理认证失败")
        host_bytes = target_host.encode("idna")
        if len(host_bytes) > 255:
            raise OSError("目标主机名过长")
        sock.sendall(bytes([5, 1, 0, 3, len(host_bytes)]) + host_bytes + target_port.to_bytes(2, "big"))
        version, reply, _, atyp = _recv_exact(sock, 4)
        if version != 5 or reply != 0:
            raise OSError(f"SOCKS5 连接失败（{reply}）")
        if atyp == 1:
            _recv_exact(sock, 4)
        elif atyp == 3:
            length = _recv_exact(sock, 1)[0]
            _recv_exact(sock, length)
        elif atyp == 4:
            _recv_exact(sock, 16)
        else:
            raise OSError("SOCKS5 响应地址类型无效")
        _recv_exact(sock, 2)
        return sock
    except Exception:
        sock.close()
        raise


class SocksHTTPConnection(http.client.HTTPConnection):
    def __init__(self, spec: ProxySpec, target_host: str, target_port: int, timeout: float):
        super().__init__(target_host, target_port, timeout=timeout)
        self._proxy_spec = spec

    def connect(self) -> None:
        self.sock = _socks5_socket(self._proxy_spec, self.host, self.port, self.timeout)


class SocksHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, spec: ProxySpec, target_host: str, target_port: int, timeout: float):
        super().__init__(target_host, target_port, timeout=timeout, context=ssl.create_default_context())
        self._proxy_spec = spec

    def connect(self) -> None:
        self.sock = _socks5_socket(self._proxy_spec, self.host, self.port, self.timeout)
        try:
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        except Exception:
            self.sock.close()
            self.sock = None
            raise


def proxy_connection(
    spec: ProxySpec,
    parsed: urllib.parse.ParseResult,
    timeout: float,
) -> Tuple[http.client.HTTPConnection, str]:
    spec = parse_proxy_url(spec)
    if spec is None:
        raise ValueError("代理地址为空")
    scheme = str(spec.scheme or "").lower()
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if scheme in {"socks5", "socks5h"}:
        if parsed.scheme == "https":
            return SocksHTTPSConnection(spec, host, port, timeout), "origin-form"
        return SocksHTTPConnection(spec, host, port, timeout), "origin-form"
    if parsed.scheme == "https":
        connection = http.client.HTTPSConnection(spec.host, spec.port, timeout=timeout, context=ssl.create_default_context())
        tunnel_headers = {}
        if spec.username or spec.password:
            token = base64.b64encode(f"{spec.username}:{spec.password}".encode()).decode("ascii")
            tunnel_headers["Proxy-Authorization"] = f"Basic {token}"
        connection.set_tunnel(host, port, headers=tunnel_headers)
        return connection, "origin-form"
    return http.client.HTTPConnection(spec.host, spec.port, timeout=timeout), "absolute-form"


def proxy_headers(spec: ProxySpec) -> dict[str, str]:
    if str(spec.scheme or "").lower() == "http" and (spec.username or spec.password):
        token = base64.b64encode(f"{spec.username}:{spec.password}".encode()).decode("ascii")
        return {"Proxy-Authorization": f"Basic {token}"}
    return {}
