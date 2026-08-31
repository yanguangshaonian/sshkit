"""安全的 SSH 客户端工具."""

from .client import (
    CommandResult,
    SshClient,
    SshError,
    SshErrorKind,
    SshLoopHandler,
)

__all__ = [
    "CommandResult",
    "SshClient",
    "SshError",
    "SshErrorKind",
    "SshLoopHandler",
]

__version__ = "0.1.0"
