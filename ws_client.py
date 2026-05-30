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

    def set_outbound_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._on_outbound = handler

    def set_config_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._on_config = handler

    @property
    def connected(self) -> bool:
        return self._connected

    async def _cleanup(self) -> None:
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

    async def connect(self) -> None:
        await self._cleanup()
        retry = 0
        while self._should_run:
            retry += 1
            try:
                self._reader, self._writer = await asyncio.open_connection(
                    self._host, self._port,
                )
                self._connected = True
                logger.info("已连接到插件服务器 %s:%s", self._host, self._port)
                return
            except (ConnectionError, OSError) as e:
                logger.warning("连接失败 (%s)，%s 秒后重试 (第%d次)...", e, self._reconnect_delay, retry)
                await asyncio.sleep(self._reconnect_delay)
            except Exception as e:
                logger.error("连接异常 (%s)，%s 秒后重试", e, self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)

    async def run(self) -> None:
        while self._should_run:
            await self.connect()
            if not self._should_run or not self._connected:
                break
            await self.request_config()
            logger.info("连接已建立，开始接收消息")
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
                except (asyncio.IncompleteReadError, ConnectionError, OSError, json.JSONDecodeError, AttributeError) as e:
                    logger.warning("与插件服务器断开: %s", e)
                    self._connected = False
                    await self._cleanup()
            if self._should_run:
                logger.info("%s 秒后重连 %s:%s ...", self._reconnect_delay, self._host, self._port)
                await asyncio.sleep(self._reconnect_delay)

    async def request_config(self) -> bool:
        return await self.send_inbound({"type": "sync_config"})

    async def send_inbound(self, data: dict[str, Any]) -> bool:
        if not self._connected or self._writer is None:
            logger.debug("未连接，丢弃入站消息")  # DEBUG 级别，不刷屏
            return False
        try:
            raw = json.dumps(data, ensure_ascii=False)
            payload = raw.encode("utf-8")
            self._writer.write(len(payload).to_bytes(4, "big"))
            self._writer.write(payload)
            await self._writer.drain()
            return True
        except Exception as e:
            logger.warning("发送入站消息失败: %s", e)
            self._connected = False
            await self._cleanup()
            return False

    async def stop(self) -> None:
        self._should_run = False
        self._connected = False
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        logger.info("插件客户端已停止")
