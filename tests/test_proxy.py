import base64
import hashlib
import http.client
import io
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import pytest

import lunatvsource_test.cms as cms_module
import lunatvsource_test.downloader as downloader_module
import lunatvsource_test.m3u8_engine as m3u8_module
import lunatvsource_test.proxy as proxy_module
from lunatvsource_test import LunaTVSource
from lunatvsource_test.cms import _fetch_public_url
from lunatvsource_test.m3u8_engine import (
    EngineSpec,
    ManagedBinaryInstaller,
    ReleaseAsset,
)
from lunatvsource_test.proxy import (
    ProxyAuthenticationError,
    ProxyConnectionError,
    ProxyResponseError,
    ProxyTargetTLSError,
    ProxyTargetTimeout,
    _socks5_socket,
    parse_proxy_url,
)


def test_parse_proxy_url_and_redacts_credentials():
    spec = parse_proxy_url("socks5://user:p%40ss@127.0.0.1:7890")
    assert spec is not None
    assert spec.scheme == "socks5"
    assert spec.host == "127.0.0.1"
    assert spec.port == 7890
    assert spec.username == "user"
    assert spec.password == "p@ss"
    assert spec.redacted == "socks5://<redacted>@127.0.0.1:7890"


def test_parse_proxy_url_normalizes_socks5h_and_formats_ipv6():
    spec = parse_proxy_url("SOCKS5H://user:p%40ss@[::1]")
    assert spec is not None
    assert spec.scheme == "socks5"
    assert spec.port == 7890
    assert spec.redacted == "socks5://<redacted>@[::1]:7890"
    assert parse_proxy_url(spec) is spec


@pytest.mark.parametrize(
    "value",
    [
        "ftp://127.0.0.1:21",
        "http://",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:7890/path",
        "http://127.0.0.1:7890/?token=secret",
    ],
)
def test_parse_proxy_url_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        parse_proxy_url(value)


def test_http_proxy_receives_absolute_form_request():
    seen = []

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append(self.path)
            payload = b"through-proxy"
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        proxy_url = f"http://127.0.0.1:{server.server_port}"
        payload, final_url = _fetch_public_url(
            "http://127.0.0.1:34567/media.m3u8",
            3,
            1024,
            ("127.0.0.0/8",),
            proxy_url=proxy_url,
        )
        assert payload == b"through-proxy"
        assert final_url == "http://127.0.0.1:34567/media.m3u8"
        assert seen == ["http://127.0.0.1:34567/media.m3u8"]
    finally:
        server.shutdown()
        server.server_close()


def test_http_proxy_redirects_keep_absolute_form_and_auth(monkeypatch):
    seen = []

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append((self.path, self.headers.get("Proxy-Authorization")))
            if len(seen) == 1:
                self.send_response(302)
                self.send_header("Location", "/final.m3u8")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = b"redirected"
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        cms_module,
        "_resolve_public_probe_target",
        lambda value, _ranges: (urlsplit(value), "93.184.216.34", 80),
    )
    try:
        proxy_url = f"http://user:secret@127.0.0.1:{server.server_port}"
        payload, final_url = _fetch_public_url(
            "http://video.example/第1集/播放.m3u8?token=值",
            3,
            1024,
            proxy_url=proxy_url,
        )
        expected_auth = "Basic " + base64.b64encode(b"user:secret").decode("ascii")
        assert payload == b"redirected"
        assert final_url == "http://video.example/final.m3u8"
        assert [item[1] for item in seen] == [expected_auth, expected_auth]
        assert seen[0][0].startswith("http://video.example/%E7%AC%AC1%E9%9B%86/")
        assert seen[0][0].endswith("?token=%E5%80%BC")
        assert seen[1][0] == "http://video.example/final.m3u8"
    finally:
        server.shutdown()
        server.server_close()


def test_https_http_proxy_uses_connect_and_keeps_auth_out_of_target_headers(monkeypatch):
    calls = {}

    class Response:
        status = 200

        @staticmethod
        def read(_limit):
            return b"ok"

        @staticmethod
        def getheader(_name):
            return None

    class FakeHTTPSConnection:
        def __init__(self, host, port, *, timeout, context):
            calls["proxy"] = (host, port, timeout, context)

        def set_tunnel(self, host, port, headers=None):
            calls["tunnel"] = (host, port, headers or {})

        def request(self, method, path, headers):
            calls["request"] = (method, path, headers)

        @staticmethod
        def getresponse():
            return Response()

        @staticmethod
        def close():
            calls["closed"] = True

    monkeypatch.setattr(proxy_module.http.client, "HTTPSConnection", FakeHTTPSConnection)
    monkeypatch.setattr(
        cms_module,
        "_resolve_public_probe_target",
        lambda value, _ranges: (urlsplit(value), "93.184.216.34", 443),
    )
    payload, _ = _fetch_public_url(
        "https://cdn.example/playlist.m3u8",
        3,
        1024,
        proxy_url="http://user:secret@proxy.example:7890",
    )
    assert payload == b"ok"
    assert calls["tunnel"][:2] == ("cdn.example", 443)
    assert calls["tunnel"][2] == {
        "Proxy-Authorization": "Basic "
        + base64.b64encode(b"user:secret").decode("ascii")
    }
    assert calls["request"][:2] == ("GET", "/playlist.m3u8")
    assert "Proxy-Authorization" not in calls["request"][2]


def test_https_proxy_errors_are_classified_without_leaking_credentials(monkeypatch):
    class FakeHTTPSConnection:
        def __init__(self, *_args, **_kwargs):
            pass

        def set_tunnel(self, *_args, **_kwargs):
            return None

        def request(self, *_args, **_kwargs):
            raise socket.timeout("proxy=secret")

        @staticmethod
        def close():
            return None

    monkeypatch.setattr(proxy_module.http.client, "HTTPSConnection", FakeHTTPSConnection)
    monkeypatch.setattr(
        cms_module,
        "_resolve_public_probe_target",
        lambda value, _ranges: (urlsplit(value), "93.184.216.34", 443),
    )
    with pytest.raises(ProxyTargetTimeout, match="目标站点超时") as error:
        _fetch_public_url(
            "https://cdn.example/playlist.m3u8",
            3,
            1024,
            proxy_url="http://user:secret@proxy.example:7890",
        )
    assert "secret" not in str(error.value)


def test_https_proxy_tls_errors_are_classified(monkeypatch):
    class FakeHTTPSConnection:
        def __init__(self, *_args, **_kwargs):
            pass

        def set_tunnel(self, *_args, **_kwargs):
            return None

        def request(self, *_args, **_kwargs):
            raise ssl.SSLError("certificate mismatch")

        @staticmethod
        def close():
            return None

    monkeypatch.setattr(proxy_module.http.client, "HTTPSConnection", FakeHTTPSConnection)
    monkeypatch.setattr(
        cms_module,
        "_resolve_public_probe_target",
        lambda value, _ranges: (urlsplit(value), "93.184.216.34", 443),
    )
    with pytest.raises(ProxyTargetTLSError, match="TLS"):
        _fetch_public_url(
            "https://cdn.example/playlist.m3u8",
            3,
            1024,
            proxy_url="http://proxy.example:7890",
        )


def test_https_proxy_407_is_authentication_error(monkeypatch):
    class Response:
        status = 407

        @staticmethod
        def getheader(_name):
            return None

    class FakeHTTPSConnection:
        def __init__(self, *_args, **_kwargs):
            pass

        def set_tunnel(self, *_args, **_kwargs):
            return None

        @staticmethod
        def request(*_args, **_kwargs):
            return None

        @staticmethod
        def getresponse():
            return Response()

        @staticmethod
        def close():
            return None

    monkeypatch.setattr(proxy_module.http.client, "HTTPSConnection", FakeHTTPSConnection)
    monkeypatch.setattr(
        cms_module,
        "_resolve_public_probe_target",
        lambda value, _ranges: (urlsplit(value), "93.184.216.34", 443),
    )
    with pytest.raises(ProxyAuthenticationError, match="407"):
        _fetch_public_url(
            "https://cdn.example/playlist.m3u8",
            3,
            1024,
            proxy_url="http://proxy.example:7890",
        )


def test_socks5_uses_domain_address_at_proxy(monkeypatch):
    class FakeSocket:
        def __init__(self):
            self.sent = []
            self.incoming = bytearray(
                bytes([5, 0])
                + bytes([5, 0, 0, 1])
                + socket.inet_aton("127.0.0.1")
                + (80).to_bytes(2, "big")
            )

        def sendall(self, data):
            self.sent.append(bytes(data))

        def recv(self, size):
            chunk = bytes(self.incoming[:size])
            del self.incoming[:size]
            return chunk

        def close(self):
            return None

    fake_socket = FakeSocket()
    monkeypatch.setattr(
        "lunatvsource_test.proxy.socket.create_connection",
        lambda _address, _timeout: fake_socket,
    )
    spec = parse_proxy_url("socks5://127.0.0.1:7890")
    assert spec is not None
    connected = _socks5_socket(spec, "media.example", 80, 3)
    connected.close()
    assert fake_socket.sent[0] == bytes([5, 1, 0])
    assert fake_socket.sent[1][:4] == bytes([5, 1, 0, 3])
    assert fake_socket.sent[1][5:18] == b"media.example"


def test_socks5_allows_proxy_side_dns_when_local_resolution_is_unavailable(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise OSError("local DNS unavailable")

    monkeypatch.setattr(cms_module.socket, "getaddrinfo", unavailable)
    parsed, address, port = cms_module._resolve_public_probe_target(
        "https://media.example/playlist.m3u8",
        allow_unresolved=True,
    )
    assert parsed.hostname == "media.example"
    assert address == "media.example"
    assert port == 443

    monkeypatch.setattr(
        cms_module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443))
        ],
    )
    with pytest.raises(ValueError, match="non-public"):
        cms_module._resolve_public_probe_target(
            "https://media.example/playlist.m3u8",
            allow_unresolved=True,
        )


def test_socks5_username_password_authentication(monkeypatch):
    client, server = socket.socketpair()
    errors = []

    def recv_exact(size):
        data = bytearray()
        while len(data) < size:
            data.extend(server.recv(size - len(data)))
        return bytes(data)

    def proxy_side():
        try:
            greeting = recv_exact(4)
            assert greeting == bytes([5, 2, 0, 2])
            server.sendall(bytes([5, 2]))
            auth = recv_exact(2)
            assert auth[0] == 1
            username = recv_exact(auth[1])
            password_length = recv_exact(1)[0]
            password = recv_exact(password_length)
            assert username == b"user"
            assert password == b"secret"
            server.sendall(bytes([1, 0]))
            request = recv_exact(5)
            assert request[:4] == bytes([5, 1, 0, 3])
            host_length = request[4]
            host = recv_exact(host_length)
            port = recv_exact(2)
            assert host == b"media.example"
            assert port == (443).to_bytes(2, "big")
            server.sendall(bytes([5, 0, 0, 1]) + socket.inet_aton("127.0.0.1") + (443).to_bytes(2, "big"))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            server.close()

    worker = threading.Thread(target=proxy_side, daemon=True)
    worker.start()
    # Replace only proxy.py's module reference.  Patching the shared stdlib
    # socket module lets unrelated background health checks borrow this test
    # socket and close it while the SOCKS handshake is still in progress.
    monkeypatch.setattr(
        proxy_module,
        "socket",
        SimpleNamespace(create_connection=lambda _address, _timeout: client),
    )
    spec = parse_proxy_url("socks5://user:secret@127.0.0.1:7890")
    assert spec is not None
    connected = _socks5_socket(spec, "media.example", 443, 3)
    connected.close()
    worker.join(timeout=1)
    assert errors == []


def test_segment_proxy_forwards_range_through_http_proxy(monkeypatch):
    seen = []

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append((self.path, self.headers.get("Range")))
            payload = b"segment"
            self.send_response(206)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Range", "bytes 2-8/9")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        cms_module,
        "_resolve_public_probe_target",
        lambda value, _ranges: (urlsplit(value), "93.184.216.34", 80),
    )
    try:
        proxy_url = f"http://127.0.0.1:{server.server_port}"
        with downloader_module._SegmentProxy(
            ("127.0.0.0/8",), proxy_url=proxy_url
        ) as segment_proxy:
            local_url = segment_proxy.url_for("http://cdn.example/segment.ts")
            response = urlopen(Request(local_url, headers={"Range": "bytes=2-8"}))
            try:
                assert response.status == 206
                assert response.read() == b"segment"
                assert response.headers["Content-Range"] == "bytes 2-8/9"
            finally:
                response.close()
        assert seen == [("http://cdn.example/segment.ts", "bytes=2-8")]
    finally:
        server.shutdown()
        server.server_close()


def test_download_queue_and_status_keep_proxy_secret_private(tmp_path: Path):
    plugin = LunaTVSource()
    plugin.init_plugin(
        {
            "enabled": False,
            "download_proxy": "http://user:secret@127.0.0.1:7890",
        }
    )
    try:
        assert plugin._config["download_proxy"] == "http://user:secret@127.0.0.1:7890"
        assert plugin._queue is not None
        assert plugin._queue._download_proxy is not None
        assert plugin._queue._download_proxy.password == "secret"
        settings = plugin.api_status()["data"]["download_settings"]
        assert settings["proxy_enabled"] is True
        assert settings["proxy_endpoint"] == "http://<redacted>@127.0.0.1:7890"
        assert "secret" not in str(plugin.api_status())
    finally:
        plugin.stop_service()


def test_managed_binary_download_uses_proxy_and_deadline(monkeypatch, tmp_path: Path):
    payload = b"proxy archive"
    asset = ReleaseAsset(
        "tool.tar.gz",
        hashlib.sha256(payload).hexdigest(),
        hashlib.sha256(payload).hexdigest(),
        "https://github.com/example/tool.tar.gz",
    )
    spec = EngineSpec("test-tool", "tool", {("linux", "x86_64"): asset})
    installer = ManagedBinaryInstaller(
        tmp_path,
        spec,
        proxy_url="http://proxy.example:7890",
    )
    calls = {}

    class Connection:
        def close(self):
            calls["closed"] = True

    def fake_fetch(url, timeout, **kwargs):
        calls.update(url=url, timeout=timeout, kwargs=kwargs)
        return Connection(), io.BytesIO(payload), url

    monkeypatch.setattr(m3u8_module, "_request_public_url", fake_fetch)
    archive = installer._download_archive(asset)
    try:
        assert archive.read_bytes() == payload
        assert calls["kwargs"]["proxy_url"] == "http://proxy.example:7890"
        assert calls["kwargs"]["deadline"] > calls["timeout"]
        assert calls["kwargs"]["headers"]["User-Agent"] == "MoviePilot-LunaTV/1.0"
        assert calls["closed"] is True
    finally:
        archive.unlink(missing_ok=True)


def test_managed_binary_proxy_http_error_is_not_retried(monkeypatch, tmp_path: Path):
    asset = ReleaseAsset(
        "tool.tar.gz",
        hashlib.sha256(b"tool").hexdigest(),
        hashlib.sha256(b"tool").hexdigest(),
        "https://github.com/example/tool.tar.gz",
    )
    spec = EngineSpec("test-tool", "tool", {("linux", "x86_64"): asset})
    installer = ManagedBinaryInstaller(
        tmp_path,
        spec,
        proxy_url="http://proxy.example:7890",
    )
    calls = []

    def fail_fetch(*_args, **_kwargs):
        calls.append(True)
        raise ProxyResponseError(503)

    monkeypatch.setattr(m3u8_module, "_request_public_url", fail_fetch)
    with pytest.raises(m3u8_module.M3U8EngineInstallError, match="代理返回错误"):
        installer._download_archive(asset)
    assert len(calls) == 1
    assert list((tmp_path / "bin").glob("*.download")) == []
