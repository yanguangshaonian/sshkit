# sshkit

一个基于 Paramiko 的 SSH 客户端工具,提供安全的主机密钥校验,远程命令执行,统一异常和循环处理能力.

## 安装

```bash
python -m pip install sshkit
```

## 快速使用

SSH 主机密钥默认使用 `RejectPolicy` 校验.连接前,请确保目标主机已存在于系统 `known_hosts` 或指定的文件中.

```python
from sshkit import SshClient, SshError

client = SshClient(
	hostname="trade.example.com",
	ip="192.0.2.10",
	port=22,
	username="deploy",
	key_path="/home/deploy/.ssh/id_ed25519",
	known_hosts_path="/home/deploy/.ssh/known_hosts",
)

try:
	client.connect()
	result = client.execute("uname -a", timeout_seconds=5.0)
	print(result.exit_status)
	print(result.stdout_text)
finally:
	client.close()
```

也可以使用上下文管理器:

```python
from sshkit import SshClient

with SshClient(
	hostname="trade.example.com",
	ip="192.0.2.10",
	port=22,
	username="deploy",
	password="password",
	known_hosts_path="/home/deploy/.ssh/known_hosts",
) as client:
	result = client.execute("hostname", timeout_seconds=5.0)
```

`password` 和 `key_path` 必须二选一.加密私钥可以通过 `key_passphrase` 传入 passphrase.程序不会自动使用 `ssh-agent` 或本地默认私钥.

## 错误处理

连接和命令错误统一使用 `SshError`,通过 `SshError.kind` 区分错误类型:

```python
from sshkit import SshError, SshErrorKind

try:
	client.connect()
except SshError as error:
	if error.kind == SshErrorKind.AUTHENTICATION:
		print("认证失败")
	elif error.kind == SshErrorKind.HOST_KEY:
		print("主机密钥校验失败")
```

## 开发

```bash
python -m pip install -e .
python -m pytest
python -m build
python -m twine check dist/*
```

## 发布到 PyPI

项目提供了交互式发布脚本.它会读取项目元数据并提示发布目标,然后检查工具,清理旧产物,运行测试,构建 wheel/sdist,执行 `twine check`,最后上传到选定的仓库.

不要把 PyPI token 写入代码,配置文件或 git.需要认证时,脚本会交给 `twine` 处理.

直接运行并交互选择目标:

```bash
./publish.sh
```

也可以直接指定目标:

```bash
./publish.sh testpypi
./publish.sh pypi
```

只构建和校验,不上传:

```bash
./publish.sh testpypi --dry-run
```

跳过测试或保留构建产物:

```bash
./publish.sh testpypi --skip-tests
./publish.sh testpypi --keep-artifacts
```

发布流程默认会在退出时清理 `build`、`dist`、`*.egg-info`、`__pycache__`、`.pytest_cache` 和 `*.pyc`. 只清理这些临时文件而不发布:

```bash
./publish.sh --clean-only
```

`--keep-build` 仍可作为 `--keep-artifacts` 的兼容别名. 使用 `--keep-artifacts` 时, 发布产物和构建缓存会保留, 便于排查问题.

建议先上传 TestPyPI,再验证安装流程:

```bash
python -m pip install --index-url https://test.pypi.org/simple/ \
	--no-deps sshkit==0.1.0
```

上面的 TestPyPI 安装命令只安装测试仓库中的 `sshkit`.运行库代码前,请确保依赖已经从正式 PyPI 安装:

```bash
python -m pip install "paramiko>=3.4"
```

确认 TestPyPI 安装正常后,再执行:

```bash
./publish.sh pypi
```

## 许可证

MIT License,详见 [LICENSE](LICENSE).
