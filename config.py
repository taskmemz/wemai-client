from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field


@dataclass
class ConnectionConfig:
    server_host: str = "127.0.0.1"
    server_port: int = 9721
    reconnect_delay: float = 5.0


@dataclass
class WeChatConfig:
    target_chats: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=lambda: ["文件传输助手", "微信团队", "微信支付"])
    send_delay: float = 0.2
    close_weixin: bool = False


@dataclass
class LogConfig:
    level: str = "INFO"
    file: str = "wemai-client.log"
    format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


@dataclass
class ClientConfig:
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    wechat: WeChatConfig = field(default_factory=WeChatConfig)
    log: LogConfig = field(default_factory=LogConfig)


def find_config_toml() -> str:
    dirs = [
        os.path.dirname(os.path.abspath(__file__)),
        os.getcwd(),
    ]
    for d in dirs:
        p = os.path.join(d, "config.toml")
        if os.path.isfile(p):
            return p
    return os.path.join(dirs[0], "config.toml")


def load_config(path: str | None = None) -> ClientConfig:
    if path is None:
        path = find_config_toml()
    if not os.path.isfile(path):
        return ClientConfig()
    with open(path, "rb") as f:
        raw_bytes = f.read()
    if raw_bytes.startswith(b'\xef\xbb\xbf'):
        raw_bytes = raw_bytes[3:]
    try:
        raw = tomllib.loads(raw_bytes.decode("utf-8"))
    except Exception as e:
        logging.getLogger("wemai_client.config").warning("config.toml 解析失败: %s，使用默认配置", e)
        return ClientConfig()
    conn_raw = raw.get("connection", {})
    wx_raw = raw.get("wechat", {})
    log_raw = raw.get("log", {})
    return ClientConfig(
        connection=ConnectionConfig(
            server_host=str(conn_raw.get("server_host", "127.0.0.1")),
            server_port=int(conn_raw.get("server_port", 9721)),
            reconnect_delay=float(conn_raw.get("reconnect_delay", 5.0)),
        ),
        wechat=WeChatConfig(
            target_chats=list(wx_raw.get("target_chats", [])),
            excluded=list(wx_raw.get("excluded", ["文件传输助手", "微信团队", "微信支付"])),
            send_delay=float(wx_raw.get("send_delay", 0.2)),
            close_weixin=bool(wx_raw.get("close_weixin", False)),
        ),
        log=LogConfig(
            level=str(log_raw.get("level", "INFO")),
            file=str(log_raw.get("file", "wemai-client.log")),
            format=str(log_raw.get("format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s")),
        ),
    )
