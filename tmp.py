import socket
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

import paramiko


# =========================
# SSH 错误类型定义
# =========================
class SshErrorKind(Enum):
    NOT_CONNECTED = "not_connected"
    CONNECTION = "connection"
    AUTHENTICATION = "authentication"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"


# =========================
# SSH 统一异常定义
# 使用 kind 区分错误类型，避免定义过多异常子类
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

        if self.kind == SshErrorKind.CONNECTION:
            return "SSH 连接失败"

        if self.kind == SshErrorKind.NOT_CONNECTED:
            return "SSH 尚未连接"

        if self.kind == SshErrorKind.TIMEOUT:
            return "SSH 操作超时"

        if self.kind == SshErrorKind.TRANSPORT:
            return "SSH 传输异常"

        return "SSH 未知异常"

    def build_alert_message(self, host_name: str, ip:str, port: int) -> str:
        return f"{self.title_text()}: ({host_name} {ip}:{port}), 错误: {self}"


# =========================
# 命令执行结果
# =========================
@dataclass(frozen=True)
class CommandResult:
    exit_status: int
    stdout_text: str
    stderr_text: str


# =========================
# 循环处理器基类
# handler 自己保存首次运行状态和上一轮错误
# run 和 on_error 统一签名
# =========================
class SshLoopHandler:
    def __init__(self) -> None:
        self.is_first_run = True
        self.last_error: Optional[Exception] = None

    def run(self, ssh_client: "SshClient") -> None:
        raise NotImplementedError("子类必须实现 run 方法")

    def on_error(self, ssh_client: "SshClient") -> None:
        raise NotImplementedError("子类必须实现 on_error 方法")


class SshClient:
    def __init__(
        self,
        hostname: str,
        ip: str,
        port: int,

        username: str,
        password: Optional[str] = None,
        key_path: Optional[str] = None,
        connect_timeout_seconds: float = 3.0,
        keepalive_interval_seconds: int = 15,
    ) -> None:
        if not password and not key_path:
            raise ValueError("必须提供 password 或 key_path")

        self.hostname = hostname
        self.ip = ip
        self.port = port


        self._username = username
        self._password = password
        self._key_path = key_path
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
    # 这里只做轻量检查，不保证下一次操作一定成功
    # =========================
    def is_connected(self) -> bool:
        if self._client is None:
            return False

        transport = self._client.get_transport()
        return transport is not None and transport.is_active()

    # =========================
    # 建立 SSH 连接
    # 这里只负责建立连接，不负责重试策略
    # =========================
    def connect(self) -> None:
        if self.is_connected():
            return

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname": self.ip,
            "port": self.port,
            "username": self._username,
            "timeout": self._connect_timeout_seconds,
        }

        try:
            if self._key_path is not None:
                connect_kwargs["pkey"] = self._load_private_key(self._key_path)  # type: ignore
            else:
                connect_kwargs["password"] = self._password  # type: ignore

            client.connect(**connect_kwargs)  # type: ignore

            transport = client.get_transport()
            if transport is not None:
                transport.set_keepalive(self._keepalive_interval_seconds)

            self._client = client

        except paramiko.AuthenticationException as exc:
            client.close()
            raise SshError(
                kind=SshErrorKind.AUTHENTICATION,
                message=f"SSH 认证失败: {exc}",
                cause=exc,
            ) from exc

        except socket.timeout as exc:
            client.close()
            raise SshError(
                kind=SshErrorKind.TIMEOUT,
                message=f"SSH 连接超时: {exc}",
                cause=exc,
            ) from exc

        except (socket.error, paramiko.SSHException) as exc:
            client.close()
            raise SshError(
                kind=SshErrorKind.CONNECTION,
                message=f"SSH 连接失败: {exc}",
                cause=exc,
            ) from exc

    # =========================
    # 关闭 SSH 连接
    # 保持幂等，允许重复调用
    # =========================
    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # =========================
    # 执行远端命令
    # 不自动连接、不自动重试、不自动关闭
    # =========================
    def execute(
        self,
        command: str,
        env: Optional[Dict[str, str]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> CommandResult:
        if self._client is None or not self.is_connected():
            raise SshError(
                kind=SshErrorKind.NOT_CONNECTED,
                message="SSH 连接尚未建立或已失活，请由上层决定是否重连",
            )

        try:
            _, stdout, stderr = self._client.exec_command(
                command,
                timeout=timeout_seconds,
                environment=env,
            )

            exit_status = stdout.channel.recv_exit_status()
            stdout_text = stdout.read().decode("utf-8", errors="replace")
            stderr_text = stderr.read().decode("utf-8", errors="replace")

            return CommandResult(
                exit_status=exit_status,
                stdout_text=stdout_text,
                stderr_text=stderr_text,
            )

        except socket.timeout as exc:
            raise SshError(
                kind=SshErrorKind.TIMEOUT,
                message=f"SSH 命令执行超时: {exc}",
                cause=exc,
            ) from exc

        except (socket.error, EOFError, paramiko.SSHException) as exc:
            raise SshError(
                kind=SshErrorKind.TRANSPORT,
                message=f"SSH 传输异常: {exc}",
                cause=exc,
            ) from exc

    # =========================
    # 循环运行入口
    # handler 自己保存首次运行状态和最近一次错误
    # =========================
    def loop_run(
        self,
        handler: SshLoopHandler,
        interval_seconds: float,
    ) -> None:
        while True:
            try:
                if not self.is_connected():
                    self.connect()
                handler.run(self)
                handler.last_error = None
            except Exception as exc:
                handler.last_error = exc
                if isinstance(exc, SshError) and self._should_reset_connection(exc):
                    self.close()
                handler.on_error(self)
            finally:
                handler.is_first_run = False

            if interval_seconds <= 0:
                break
            time.sleep(interval_seconds)

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
    def _load_private_key(key_path: str):
        key_loaders = [
            paramiko.Ed25519Key.from_private_key_file,
            paramiko.RSAKey.from_private_key_file,
            paramiko.ECDSAKey.from_private_key_file,
        ]

        last_error: Optional[Exception] = None

        for key_loader in key_loaders:
            try:
                return key_loader(key_path)
            except Exception as exc:
                last_error = exc

        raise SshError(
            kind=SshErrorKind.CONNECTION,
            message=f"无法加载私钥文件: {key_path}",
            cause=last_error,
        ) from last_error