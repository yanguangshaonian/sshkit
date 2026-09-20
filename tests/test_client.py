import socket
import time
from unittest.mock import Mock

import paramiko
import pytest

from sshkit import CommandResult, SshClient, SshError, SshErrorKind


@pytest.mark.parametrize(
    ("kind", "value", "title"),
    [
        (SshErrorKind.NOT_CONNECTED, "not_connected", "SSH 尚未连接"),
        (SshErrorKind.CONNECTION, "connection", "SSH 连接失败"),
        (SshErrorKind.AUTHENTICATION, "authentication", "SSH 认证失败"),
        (SshErrorKind.KEY_LOAD, "key_load", "SSH 私钥加载失败"),
        (SshErrorKind.TIMEOUT, "timeout", "SSH 操作超时"),
        (SshErrorKind.TRANSPORT, "transport", "SSH 传输异常"),
    ],
)
def test_error_kind_and_message(kind, value, title):
    cause = ValueError("测试错误")
    message = f"[example-client 192.0.2.10:2222] {title}: {cause}"
    error = SshError("example-client", "192.0.2.10", 2222, kind, str(cause), cause)

    assert str(kind) == title
    assert kind.value == value
    assert str(error) == message
    assert error.args == (message,)
    assert error.client_name == "example-client"
    assert error.ip == "192.0.2.10"
    assert error.port == 2222
    assert error.kind is kind
    assert error.cause is cause
    assert str(SshError("example-client", "192.0.2.10", 2222, kind, "")) == (
        f"[example-client 192.0.2.10:2222] {title}"
    )


@pytest.mark.parametrize("missing", ["client_name", "ip", "port"])
def test_error_requires_identity(missing):
    arguments = {
        "client_name": "example-client",
        "ip": "192.0.2.10",
        "port": 2222,
        "kind": SshErrorKind.NOT_CONNECTED,
        "message": "连接未建立",
    }
    del arguments[missing]

    with pytest.raises(TypeError, match=missing):
        SshError(**arguments)


def test_error_rejects_old_signature():
    with pytest.raises(TypeError):
        SshError(SshErrorKind.NOT_CONNECTED, "连接未建立")


def test_run_once_without_connection_preserves_identity():
    client = SshClient("example-client", "192.0.2.10", 2222, "user", password="x")

    with pytest.raises(SshError) as error_info:
        client.run_once("command")

    error = error_info.value
    assert (error.client_name, error.ip, error.port) == (
        "example-client", "192.0.2.10", 2222
    )
    assert error.kind is SshErrorKind.NOT_CONNECTED
    assert error.cause is None
    assert error.__cause__ is None
    assert str(error) == (
        "[example-client 192.0.2.10:2222] SSH 尚未连接: "
        "连接未建立或已失活,请由上层决定是否重连"
    )


class FakeTransport:
    def __init__(self, channel):
        self.channel = channel
        self.active = True

    def is_active(self):
        return self.active

    def open_session(self, timeout=None):
        return self.channel

    def set_keepalive(self, interval):
        self.keepalive_interval = interval


class FakeStream:
    def __init__(self, channel):
        self.channel = channel
        self.closed = False

    def close(self):
        self.closed = True


class FakeChannel:
    def __init__(
        self,
        stdout_chunks=None,
        stderr_chunks=None,
        never_exits=False,
        exit_status=0,
    ):
        self.stdout_chunks = list(stdout_chunks or [])
        self.stderr_chunks = list(stderr_chunks or [])
        self.never_exits = never_exits
        self.closed = False
        self.streams = []
        self.exit_status = exit_status

    def settimeout(self, timeout):
        self.timeout = timeout

    def update_environment(self, environment):
        self.environment = environment

    def exec_command(self, command):
        self.command = command

    def makefile_stdin(self, mode, bufsize):
        stream = FakeStream(self)
        self.streams.append(stream)
        return stream

    def recv_ready(self):
        return bool(self.stdout_chunks)

    def recv_stderr_ready(self):
        return bool(self.stderr_chunks)

    def recv(self, size):
        return self.stdout_chunks.pop(0)

    def recv_stderr(self, size):
        return self.stderr_chunks.pop(0)

    def exit_status_ready(self):
        return not self.never_exits and not self.stdout_chunks and not self.stderr_chunks

    def recv_exit_status(self):
        return self.exit_status

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, channel):
        self.channel = channel
        self.transport = FakeTransport(channel)
        self.closed = False

    def get_transport(self):
        return self.transport

    def close(self):
        self.closed = True
        self.transport.active = False
        self.channel.close()


class FakeConnectClient:
    def __init__(self, connect_error=None):
        self.transport = FakeTransport(FakeChannel())
        self.connect_error = connect_error
        self.closed = False

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        if self.connect_error is not None:
            raise self.connect_error

    def get_transport(self):
        return self.transport

    def close(self):
        self.closed = True
        self.transport.active = False


def make_client(channel):
    client = SshClient("host", "127.0.0.1", 22, "user", password="password")
    client._client = FakeClient(channel)
    return client


def test_public_command_result_and_dual_stream_execution():
    channel = FakeChannel([b"out-1", b"out-2"], [b"err"])
    result = make_client(channel).run_once("printf test", timeout_seconds=1.0)

    assert isinstance(result, CommandResult)
    assert result.exit_status == 0
    assert result.stdout_text == "out-1out-2"
    assert result.stderr_text == "err"
    assert channel.closed
    assert all(stream.closed for stream in channel.streams)


def test_run_once_preserves_exit_status_and_environment():
    channel = FakeChannel([b"out"], [b"err"], exit_status=7)
    result = make_client(channel).run_once(
        "command",
        env={"LANG": "C"},
        timeout_seconds=1.0,
    )

    assert result.exit_status == 7
    assert channel.environment == {"LANG": "C"}


def test_run_once_timeout_closes_client_and_channel():
    channel = FakeChannel(never_exits=True)
    client = make_client(channel)

    with pytest.raises(SshError) as error_info:
        client.run_once("hang", timeout_seconds=0.03)

    assert error_info.value.kind == SshErrorKind.TIMEOUT
    assert str(error_info.value) == "[host 127.0.0.1:22] SSH 操作超时: 执行命令 hang"
    assert client._client.closed
    assert channel.closed


def test_invalid_configuration_is_rejected():
    with pytest.raises(ValueError):
        SshClient("host", "127.0.0.1", 0, "user", password="password")
    with pytest.raises(ValueError):
        SshClient("host", "127.0.0.1", 22, "user")
    with pytest.raises(ValueError):
        SshClient(
            "host",
            "127.0.0.1",
            22,
            "user",
            password="password",
            key_path="key",
        )


def test_client_name_is_stored_for_diagnostics():
    client = SshClient(
        client_name="example-client",
        ip="127.0.0.1",
        port=22,
        username="user",
        password="password",
    )

    assert client.client_name == "example-client"


def test_connect_uses_ip_and_configures_keepalive(monkeypatch):
    paramiko_client = FakeConnectClient()
    monkeypatch.setattr(paramiko, "SSHClient", lambda: paramiko_client)
    client = SshClient(
        client_name="example-client",
        ip="192.0.2.10",
        port=2222,
        username="user",
        password="password",
        connect_timeout_seconds=2.5,
        keepalive_interval_seconds=9,
    )

    client.connect()

    assert client.is_connected()
    assert isinstance(paramiko_client.policy, paramiko.AutoAddPolicy)
    assert paramiko_client.connect_kwargs == {
        "hostname": "192.0.2.10",
        "port": 2222,
        "username": "user",
        "timeout": 2.5,
        "auth_timeout": 2.5,
        "banner_timeout": 2.5,
        "allow_agent": False,
        "look_for_keys": False,
        "password": "password",
    }
    assert paramiko_client.transport.keepalive_interval == 9


@pytest.mark.parametrize(
    "credentials",
    [
        {"password": "password"},
        {"key_path": "private_key"},
        {"key_path": "private_key", "key_passphrase": "passphrase"},
    ],
)
def test_connect_accepts_unknown_host_without_host_key_files(monkeypatch, credentials):
    paramiko_client = paramiko.SSHClient()
    transport = Mock()
    server_key = Mock()
    server_key.get_name.return_value = "ssh-ed25519"
    server_key.get_fingerprint.return_value = b"fingerprint"
    private_key = Mock()
    load_private_key = Mock(return_value=private_key)
    monkeypatch.setattr(SshClient, "_load_private_key", load_private_key)
    monkeypatch.setattr(paramiko, "SSHClient", lambda: paramiko_client)
    monkeypatch.setattr(paramiko_client, "get_transport", Mock(return_value=transport))
    monkeypatch.setattr(paramiko_client, "_log", Mock())
    file_access = Mock(side_effect=AssertionError("不应访问主机密钥文件"))
    monkeypatch.setattr("builtins.open", file_access)
    for method in ("load_system_host_keys", "load_host_keys", "save_host_keys"):
        monkeypatch.setattr(paramiko_client, method, file_access)

    def accept_server_key(**kwargs):
        paramiko_client._policy.missing_host_key(
            paramiko_client, kwargs["hostname"], server_key
        )

    connect = Mock(side_effect=accept_server_key)
    monkeypatch.setattr(paramiko_client, "connect", connect)
    client = SshClient("host", "192.0.2.10", 22, "user", **credentials)

    client.connect()

    assert client.is_connected()
    assert paramiko_client.get_host_keys()["192.0.2.10"]["ssh-ed25519"] is server_key
    file_access.assert_not_called()
    kwargs = connect.call_args.kwargs
    assert kwargs["username"] == "user"
    assert kwargs["allow_agent"] is False
    assert kwargs["look_for_keys"] is False
    if "key_path" in credentials:
        load_private_key.assert_called_once_with(
            credentials["key_path"], credentials.get("key_passphrase")
        )
        assert kwargs["pkey"] is private_key
        assert "password" not in kwargs
    else:
        load_private_key.assert_not_called()
        assert kwargs["password"] == credentials["password"]
        assert "pkey" not in kwargs


@pytest.mark.parametrize(
    ("connect_error", "expected_kind"),
    [
        (paramiko.AuthenticationException("denied"), SshErrorKind.AUTHENTICATION),
        (socket.timeout("timed out"), SshErrorKind.TIMEOUT),
        (paramiko.SSHException("transport"), SshErrorKind.CONNECTION),
    ],
)
def test_connect_maps_errors_and_closes_client(
    monkeypatch,
    connect_error,
    expected_kind,
):
    paramiko_client = FakeConnectClient(connect_error=connect_error)
    monkeypatch.setattr(paramiko, "SSHClient", lambda: paramiko_client)
    client = SshClient("example-client", "192.0.2.10", 22, "user", password="x")

    with pytest.raises(SshError) as error_info:
        client.connect()

    assert error_info.value.kind == expected_kind
    assert error_info.value.cause is connect_error
    assert error_info.value.__cause__ is connect_error
    detail = (
        f"建立连接: {connect_error}"
        if expected_kind is SshErrorKind.TIMEOUT
        else str(connect_error)
    )
    assert error_info.value.client_name == "example-client"
    assert error_info.value.ip == "192.0.2.10"
    assert error_info.value.port == 22
    assert str(error_info.value) == f"[example-client 192.0.2.10:22] {expected_kind}: {detail}"
    assert paramiko_client.closed


def test_close_is_idempotent():
    client = make_client(FakeChannel())

    client.close()
    client.close()

    assert client._client is None


def test_invalid_timeout_is_rejected_before_connection_check():
    client = SshClient("host", "127.0.0.1", 22, "user", password="password")

    with pytest.raises(ValueError):
        client.run_once("command", timeout_seconds=float("nan"))
