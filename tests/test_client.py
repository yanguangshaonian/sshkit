import socket
import time

import paramiko
import pytest

from sshkit import CommandResult, SshClient, SshError, SshErrorKind


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
    def __init__(self, connect_error=None, load_error=None):
        self.transport = FakeTransport(FakeChannel())
        self.connect_error = connect_error
        self.load_error = load_error
        self.closed = False

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def load_system_host_keys(self):
        if self.load_error is not None:
            raise self.load_error

    def load_host_keys(self, path):
        self.known_hosts_path = path

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
    result = make_client(channel).execute("printf test", timeout_seconds=1.0)

    assert isinstance(result, CommandResult)
    assert result.exit_status == 0
    assert result.stdout_text == "out-1out-2"
    assert result.stderr_text == "err"
    assert channel.closed
    assert all(stream.closed for stream in channel.streams)


def test_execute_preserves_exit_status_and_environment():
    channel = FakeChannel([b"out"], [b"err"], exit_status=7)
    result = make_client(channel).execute(
        "command",
        env={"LANG": "C"},
        timeout_seconds=1.0,
    )

    assert result.exit_status == 7
    assert channel.environment == {"LANG": "C"}


def test_execute_timeout_closes_client_and_channel():
    channel = FakeChannel(never_exits=True)
    client = make_client(channel)

    with pytest.raises(SshError) as error_info:
        client.execute("hang", timeout_seconds=0.03)

    assert error_info.value.kind == SshErrorKind.TIMEOUT
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
        known_hosts_path="known_hosts",
        connect_timeout_seconds=2.5,
        keepalive_interval_seconds=9,
    )

    client.connect()

    assert client.is_connected()
    assert paramiko_client.known_hosts_path == "known_hosts"
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
    assert paramiko_client.closed


def test_close_is_idempotent():
    client = make_client(FakeChannel())

    client.close()
    client.close()

    assert client._client is None


def test_invalid_timeout_is_rejected_before_connection_check():
    client = SshClient("host", "127.0.0.1", 22, "user", password="password")

    with pytest.raises(ValueError):
        client.execute("command", timeout_seconds=float("nan"))


def test_loop_keeps_original_error_when_error_handler_fails():
    class ErrorHandler:
        is_first_run = True
        last_error = None

        def run(self, ssh_client):
            raise RuntimeError("original error")

        def on_error(self, ssh_client):
            raise RuntimeError("handler error")

    client = SshClient("host", "127.0.0.1", 22, "user", password="password")
    client._client = FakeClient(FakeChannel())
    handler = ErrorHandler()
    client.loop_run(handler, 0)

    assert str(handler.last_error) == "original error"
    assert handler.is_first_run is False
