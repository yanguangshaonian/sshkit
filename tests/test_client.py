import time

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


class FakeStream:
    def __init__(self, channel):
        self.channel = channel
        self.closed = False

    def close(self):
        self.closed = True


class FakeChannel:
    def __init__(self, stdout_chunks=None, stderr_chunks=None, never_exits=False):
        self.stdout_chunks = list(stdout_chunks or [])
        self.stderr_chunks = list(stderr_chunks or [])
        self.never_exits = never_exits
        self.closed = False
        self.streams = []

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
        return 0

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
