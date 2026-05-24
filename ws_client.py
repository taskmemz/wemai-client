from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("wemai_client.ws")


class WsPluginClient:
    def __init__(self, host: str, port: int, reconnect_delay: float = 5.0) -> None:
        self._host = host
        self._port = port
        self._reconnect_delay = reconnect_delay
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._on_outbound: Optional[Callable[[dict[str, Any]], None]] = None
        self._on_config: Optional[Callable[[dict[str, Any]], None]] = None
        self._should_run = True

    async def _cleanup(self) -> None:
        """关闭并清理旧连接，确保下次 connect 不会受残留 socket 影响"""
        self._connected = False
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        if self._reader is not None:
            self._reader = None

    def set_outbound_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._on_outbound = handler

    def set_config_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._on_config = handler

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        await self._cleanup()
        while self._should_run:
            try:
                self._reader, self._writer = await asyncio.open_connection(
                    self._host, self._port,
                )
                self._connected = True
                logger.info("已连接到插件服务器 %s:%s", self._host, self._port)
                return
            except (ConnectionError, OSError) as e:
                logger.warning("连接失败 (%s)，%s 秒后重试...", e, self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)

    async def run(self) -> None:
        while self._should_run:
            await self.connect()
            if not self._should_run or not self._connected:
                break
            await self.request_config()
            while self._should_run and self._connected:
                try:
                    raw_len = await self._reader.readexactly(4)
                    length = int.from_bytes(raw_len, "big")
                    payload = await self._reader.readexactly(length)
                    msg = json.loads(payload.decode("utf-8"))
                    if msg.get("type") in ("config_update", "sync_config"):
                        if self._on_config is not None:
                            self._on_config(msg)
                    elif self._on_outbound is not None:
                        self._on_outbound(msg)
                except (asyncio.IncompleteReadError, ConnectionError, OSError, json.JSONDecodeError) as e:
                    logger.warning("与插件服务器断开: %s", e)
                    self._connected = False
                    await self._cleanup()
            if self._should_run:
                logger.info("断线，%s 秒后重连...", self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)

    async def request_config(self) -> bool:
        return await self.send_inbound({"type": "sync_config"})

    async def send_inbound(self, data: dict[str, Any]) -> bool:
        if not self._connected or self._writer is None:
            logger.warning("未连接，丢弃入站消息")
            return False
        try:
            raw = json.dumps(data, ensure_ascii=False)
            payload = raw.encode("utf-8")
            self._writer.write(len(payload).to_bytes(4, "big"))
            self._writer.write(payload)
            await self._writer.drain()
            return True
        except Exception as e:
            logger.error("发送入站消息失败: %s", e)
            await self._cleanup()
            return False

    async def stop(self) -> None:
        self._should_run = False
        await self._cleanup()
        logger.info("插件客户端已停止")
