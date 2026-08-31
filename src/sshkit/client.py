import socket
import threading
import time
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Dict, Optional

import paramiko


# =========================
# SSH 错误类型定义
# =========================
class SshErrorKind(Enum):
    NOT_CONNECTED = "not_connected"
    CONNECTION = "connection"
    AUTHENTICATION = "authentication"
    KEY_LOAD = "key_load"
    HOST_KEY = "host_key"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"


# =========================
# SSH 统一异常定义
# 使用 kind 区分错误类型,避免定义过多异常子类
# =========================
class SshError(Exception):
    def __init__(
        self,
        kind: SshErrorKind,
        message: str,
        cause: Optional[Exception] = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.cause = cause

    def __str__(self) -> str:
        return self.message

    def title_text(self) -> str:
        if self.kind == SshErrorKind.AUTHENTICATION:
            return "SSH 认证失败"
        if self.kind == SshErrorKind.KEY_LOAD:
            return "SSH 私钥加载失败"
        if self.kind == SshErrorKind.HOST_KEY:
            return "SSH 主机密钥校验失败"
        if self.kind == SshErrorKind.CONNECTION:
            return "SSH 连接失败"
        if self.kind == SshErrorKind.NOT_CONNECTED:
            return "SSH 尚未连接"
        if self.kind == SshErrorKind.TIMEOUT:
            return "SSH 操作超时"
        if self.kind == SshErrorKind.TRANSPORT:
            return "SSH 传输异常"
        return "SSH 未知异常"

    def build_alert_message(self, client_name: str, ip: str, port: int) -> str:
        return f"{self.title_text()}: ({client_name} {ip}:{port}), 错误: {self}"


# =========================
# 命令执行结果
# =========================
@dataclass(frozen=True)
class CommandResult:
    exit_status: int
    stdout_text: str
    stderr_text: str


class _RejectUnknownHostKeyPolicy(paramiko.RejectPolicy):
    def missing_host_key(self, client, hostname, key) -> None:
        raise SshError(
            kind=SshErrorKind.HOST_KEY,
            message=f"SSH 主机密钥未在 known_hosts 中找到: {hostname}",
        )


class SshClient:
    def __init__(
        self,
        client_name: str,
        ip: str,
        port: int,
        username: str,
        password: Optional[str] = None,
        key_path: Optional[str] = None,
        connect_timeout_seconds: float = 3.0,
        keepalive_interval_seconds: int = 15,
        key_passphrase: Optional[str] = None,
        known_hosts_path: Optional[str] = None,
    ) -> None:
        if password is None and key_path is None:
            raise ValueError("必须提供 password 或 key_path")
        if password is not None and key_path is not None:
            raise ValueError("password 和 key_path 只能提供一个")
        if not 1 <= port <= 65535:
            raise ValueError("port 必须在 1 到 65535 之间")
        if not isfinite(connect_timeout_seconds) or connect_timeout_seconds <= 0:
            raise ValueError("connect_timeout_seconds 必须大于 0")
        if not isfinite(keepalive_interval_seconds) or keepalive_interval_seconds < 0:
            raise ValueError("keepalive_interval_seconds 不能小于 0")

        self.client_name = client_name
        self.ip = ip
        self.port = port
        self._username = username
        self._password = password
        self._key_path = key_path
        self._key_passphrase = key_passphrase
        self._known_hosts_path = known_hosts_path
        self._connect_timeout_seconds = connect_timeout_seconds
        self._keepalive_interval_seconds = keepalive_interval_seconds
        self._client: Optional[paramiko.SSHClient] = None

    def __enter__(self) -> "SshClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # =========================
    # 连接状态检查
    # 这里只做轻量检查,不保证下一次操作一定成功
    # =========================
    def is_connected(self) -> bool:
        if self._client is None:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()

    # =========================
    # 建立 SSH 连接
    # 这里只负责建立连接,不负责重试策略
    # =========================
    def connect(self) -> None:
        if self.is_connected():
            return
        if self._client is not None:
            self._close_quietly(self._client)
            self._client = None

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(_RejectUnknownHostKeyPolicy())
        connect_kwargs = {
            "hostname": self.ip,
            "port": self.port,
            "username": self._username,
            "timeout": self._connect_timeout_seconds,
            "auth_timeout": self._connect_timeout_seconds,
            "banner_timeout": self._connect_timeout_seconds,
            "allow_agent": False,
            "look_for_keys": False,
        }

        try:
            try:
                client.load_system_host_keys()
                if self._known_hosts_path is not None:
                    client.load_host_keys(self._known_hosts_path)
            except (OSError, UnicodeError) as exc:
                raise SshError(
                    kind=SshErrorKind.CONNECTION,
                    message=f"SSH known_hosts 配置失败: {exc}",
                    cause=exc,
                ) from exc
            except paramiko.hostkeys.InvalidHostKey as exc:
                raise SshError(
                    kind=SshErrorKind.HOST_KEY,
                    message=f"SSH known_hosts 内容无效: {exc}",
                    cause=exc,
                ) from exc

            if self._key_path is not None:
                connect_kwargs["pkey"] = self._load_private_key(
                    self._key_path,
                    self._key_passphrase,
                )
            else:
                connect_kwargs["password"] = self._password

            client.connect(**connect_kwargs)
            transport = client.get_transport()
            if transport is not None:
                transport.set_keepalive(self._keepalive_interval_seconds)
            self._client = client

        except paramiko.AuthenticationException as exc:
            self._close_quietly(client)
            raise SshError(
                kind=SshErrorKind.AUTHENTICATION,
                message=f"SSH 认证失败: {exc}",
                cause=exc,
            ) from exc
        except paramiko.BadHostKeyException as exc:
            self._close_quietly(client)
            raise SshError(
                kind=SshErrorKind.HOST_KEY,
                message=f"SSH 主机密钥校验失败: {exc}",
                cause=exc,
            ) from exc
        except SshError:
            self._close_quietly(client)
            raise
        except socket.timeout as exc:
            self._close_quietly(client)
            raise SshError(
                kind=SshErrorKind.TIMEOUT,
                message=f"SSH 连接超时: {exc}",
                cause=exc,
            ) from exc
        except (socket.error, paramiko.SSHException) as exc:
            self._close_quietly(client)
            raise SshError(
                kind=SshErrorKind.CONNECTION,
                message=f"SSH 连接失败: {exc}",
                cause=exc,
            ) from exc

    # =========================
    # 关闭 SSH 连接
    # 保持幂等,允许重复调用
    # =========================
    def close(self) -> None:
        if self._client is not None:
            self._close_quietly(self._client)
            self._client = None

    # =========================
    # 执行远端命令
    # 不自动连接,不自动重试,不自动关闭 SSH client
    # =========================
    def run_once(
        self,
        command: str,
        env: Optional[Dict[str, str]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> CommandResult:
        if (
            timeout_seconds is not None
            and (not isfinite(timeout_seconds) or timeout_seconds <= 0)
        ):
            raise ValueError("timeout_seconds 必须大于 0")
        if self._client is None or not self.is_connected():
            raise SshError(
                kind=SshErrorKind.NOT_CONNECTED,
                message="SSH 连接尚未建立或已失活,请由上层决定是否重连",
            )

        stdin = None
        channel = None
        timeout_timer = None
        timed_out = threading.Event()
        deadline = (
            time.monotonic() + timeout_seconds
            if timeout_seconds is not None
            else None
        )

        try:
            transport = self._client.get_transport()
            if transport is None or not transport.is_active():
                raise SshError(
                    kind=SshErrorKind.NOT_CONNECTED,
                    message="SSH 连接尚未建立或已失活,请由上层决定是否重连",
                )

            open_timeout = (
                max(0.0, deadline - time.monotonic())
                if deadline is not None
                else None
            )
            if deadline is not None:
                timeout_timer = threading.Timer(
                    open_timeout,
                    self._abort_client_on_timeout,
                    args=(self._client, timed_out),
                )
                timeout_timer.daemon = True
                timeout_timer.start()

            channel = transport.open_session(timeout=open_timeout)
            remaining_timeout = (
                max(0.0, deadline - time.monotonic())
                if deadline is not None
                else None
            )
            if timed_out.is_set() or self._deadline_expired(deadline):
                raise self._command_timeout(command)

            channel.settimeout(remaining_timeout)
            if env:
                channel.update_environment(env)
            channel.exec_command(command)
            stdin = channel.makefile_stdin("wb", -1)
            self._close_quietly(stdin)

            stdout_chunks = bytearray()
            stderr_chunks = bytearray()
            while True:
                for stdout_read_attempt in range(16):
                    if not channel.recv_ready():
                        break
                    stdout_chunks.extend(channel.recv(65536))
                    if timed_out.is_set() or self._deadline_expired(deadline):
                        raise self._command_timeout(command)

                for stderr_read_attempt in range(16):
                    if not channel.recv_stderr_ready():
                        break
                    stderr_chunks.extend(channel.recv_stderr(65536))
                    if timed_out.is_set() or self._deadline_expired(deadline):
                        raise self._command_timeout(command)

                if timed_out.is_set():
                    raise self._command_timeout(command)

                if channel.exit_status_ready() or channel.closed:
                    continue_reading = channel.recv_ready() or channel.recv_stderr_ready()
                    if not continue_reading:
                        break

                if timed_out.is_set() or self._deadline_expired(deadline):
                    raise self._command_timeout(command)
                time.sleep(0.01)

            if timed_out.is_set() or self._deadline_expired(deadline):
                raise self._command_timeout(command)

            exit_status = channel.recv_exit_status()
            if exit_status < 0:
                raise SshError(
                    kind=SshErrorKind.TRANSPORT,
                    message="SSH 命令未返回有效退出状态",
                )
            return CommandResult(
                exit_status=exit_status,
                stdout_text=bytes(stdout_chunks).decode("utf-8", errors="replace"),
                stderr_text=bytes(stderr_chunks).decode("utf-8", errors="replace"),
            )

        except socket.timeout as exc:
            raise SshError(
                kind=SshErrorKind.TIMEOUT,
                message=f"SSH 命令执行超时: {exc}",
                cause=exc,
            ) from exc
        except (socket.error, EOFError, paramiko.SSHException) as exc:
            if timed_out.is_set() or self._deadline_expired(deadline):
                raise self._command_timeout(command, exc) from exc
            raise SshError(
                kind=SshErrorKind.TRANSPORT,
                message=f"SSH 传输异常: {exc}",
                cause=exc,
            ) from exc
        finally:
            if timeout_timer is not None:
                timeout_timer.cancel()
            self._close_quietly(channel)
            self._close_quietly(stdin)

    @staticmethod
    def _deadline_expired(deadline: Optional[float]) -> bool:
        return deadline is not None and time.monotonic() >= deadline

    @staticmethod
    def _command_timeout(
        command: str,
        cause: Optional[Exception] = None,
    ) -> SshError:
        return SshError(
            kind=SshErrorKind.TIMEOUT,
            message=f"SSH 命令执行超时: {command}",
            cause=cause,
        )

    @staticmethod
    def _abort_client_on_timeout(
        client: paramiko.SSHClient,
        timed_out: threading.Event,
    ) -> None:
        timed_out.set()
        SshClient._close_quietly(client)

    @staticmethod
    def _close_quietly(resource) -> None:
        if resource is None:
            return
        try:
            resource.close()
        except Exception:
            pass

    # =========================
    # 判断哪些错误需要重置连接
    # =========================
    @staticmethod
    def _should_reset_connection(exc: SshError) -> bool:
        return exc.kind in {
            SshErrorKind.CONNECTION,
            SshErrorKind.NOT_CONNECTED,
            SshErrorKind.TIMEOUT,
            SshErrorKind.TRANSPORT,
        }

    # =========================
    # 加载私钥
    # 依次尝试常见私钥格式
    # =========================
    @staticmethod
    def _load_private_key(
        key_path: str,
        key_passphrase: Optional[str] = None,
    ):
        key_loaders = [
            paramiko.Ed25519Key.from_private_key_file,
            paramiko.RSAKey.from_private_key_file,
            paramiko.ECDSAKey.from_private_key_file,
        ]
        last_error: Optional[Exception] = None
        password_error: Optional[Exception] = None

        for key_loader in key_loaders:
            try:
                return key_loader(key_path, password=key_passphrase)
            except (FileNotFoundError, PermissionError, OSError) as exc:
                raise SshError(
                    kind=SshErrorKind.KEY_LOAD,
                    message=f"无法读取私钥文件: {key_path}: {exc}",
                    cause=exc,
                ) from exc
            except (paramiko.SSHException, ValueError) as exc:
                last_error = exc
                if isinstance(exc, paramiko.PasswordRequiredException):
                    password_error = exc

        if password_error is not None:
            last_error = password_error
        raise SshError(
            kind=SshErrorKind.KEY_LOAD,
            message=f"无法加载私钥文件: {key_path}",
            cause=last_error,
        ) from last_error
