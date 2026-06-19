from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field


_CONFIG_PATH: str | None = None


@dataclass
class ConnectionConfig:
    server_host: str = "127.0.0.1"
    server_port: int = 9721
    reconnect_delay: float = 5.0


@dataclass
class LogConfig:
    level: str = "INFO"
    file: str = "wemai-client.log"
    format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


@dataclass
class ClientConfig:
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    log: LogConfig = field(default_factory=LogConfig)


def find_config_toml() -> str:
    """返回配置文件的路径。找到现有文件则直接返回，否则以脚本所在目录为默认位置。"""
    global _CONFIG_PATH
    if _CONFIG_PATH is not None:
        return _CONFIG_PATH
    dirs = [
        os.path.dirname(os.path.abspath(__file__)),
        os.getcwd(),
    ]
    for d in dirs:
        p = os.path.join(d, "config.toml")
        if os.path.isfile(p):
            _CONFIG_PATH = p
            return p
    p = os.path.join(dirs[0], "config.toml")
    _CONFIG_PATH = p
    return p


def _prompt_host() -> str:
    """交互式询问服务器地址"""
    default = "127.0.0.1"
    try:
        val = input(f"  服务器地址 (默认 {default}): ").strip()
        return val or default
    except (EOFError, KeyboardInterrupt):
        return default


def _prompt_port() -> int:
    """交互式询问服务器端口"""
    default = 9721
    try:
        val = input(f"  服务器端口 (默认 {default}): ").strip()
        return int(val) if val else default
    except (EOFError, KeyboardInterrupt, ValueError):
        return default


def _make_toml(server_host: str, server_port: int) -> str:
    return f"""\
[connection]
server_host = "{server_host}"
server_port = {server_port}
reconnect_delay = 5.0

[log]
level = "INFO"
file = "wemai-client.log"
format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
"""


def _interactive_create(path: str) -> None:
    """交互式创建配置文件"""
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)

    print()
    print("=" * 50)
    print("  WeMai Client 首次启动")
    print("  请填写服务器（WeMai Adapter）的连接信息")
    print("=" * 50)
    host = _prompt_host()
    port = _prompt_port()
    print()

    with open(path, "w", encoding="utf-8") as f:
        f.write(_make_toml(host, port))

    logger = logging.getLogger("wemai_client.config")
    logger.info("配置文件已创建: %s", path)


def load_config(path: str | None = None) -> ClientConfig:
    logger = logging.getLogger("wemai_client.config")

    if path is None:
        path = find_config_toml()

    # 不存在则交互式创建
    if not os.path.isfile(path):
        _interactive_create(path)

    # 读取
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        logger.warning("无法读取配置文件 %s: %s", path, e)
        return ClientConfig()

    # 跳过 BOM
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except Exception as e:
        logger.warning("config.toml 解析失败: %s，请检查文件格式", e)
        return ClientConfig()

    conn = data.get("connection", {})
    lg = data.get("log", {})

    return ClientConfig(
        connection=ConnectionConfig(
            server_host=str(conn.get("server_host", "127.0.0.1")),
            server_port=int(conn.get("server_port", 9721)),
            reconnect_delay=float(conn.get("reconnect_delay", 5.0)),
        ),
        log=LogConfig(
            level=str(lg.get("level", "INFO")),
            file=str(lg.get("file", "wemai-client.log")),
            format=str(lg.get("format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s")),
        ),
    )
