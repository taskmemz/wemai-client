from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

logger = logging.getLogger("wemai_client.ws")


class WsPluginClient:
    def __init__(
        self, host: str, port: int, reconnect_delay: float = 5.0,
        read_timeout: float = 120.0, write_timeout: float = 60.0,
    ) -> None:
        self._host = host
        self._port = port
        self._reconnect_delay = reconnect_delay
        self._read_timeout = read_timeout
        self._write_timeout = write_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._on_outbound: Callable[[dict[str, Any]], None] | None = None
        self._on_config: Callable[[dict[str, Any]], None] | None = None
        self._should_run = True

    def set_outbound_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._on_outbound = handler

    def set_config_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._on_config = handler

    @property
    def connected(self) -> bool:
        return self._connected

    async def _read_exact(self, n: int) -> bytes:
        """带超时的精确读取，避免 readexactly 永久阻塞"""
        try:
            return await asyncio.wait_for(
                self._reader.readexactly(n), timeout=self._read_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "⚠️ 读取 %d 字节超时（已等待 %.1fs），疑似服务端或网络卡顿",
                n, self._read_timeout,
            )
            raise ConnectionError(f"read {n} bytes timeout after {self._read_timeout}s")

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
                delay = min(self._reconnect_delay * (2 ** (retry - 1)), 30.0)
                logger.warning("连接失败 (%s)，%.1f 秒后重试 (第%d次)...", e, delay, retry)
                await asyncio.sleep(delay)
            except Exception as e:
                logger.error("连接异常 (%s)，%s 秒后重试", e, self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)

    async def run(self) -> None:
        backoff = 0
        while self._should_run:
            await self.connect()
            if not self._should_run or not self._connected:
                break
            backoff = 0
            await self.request_config()
            logger.info("连接已建立，开始接收消息")
            while self._should_run and self._connected:
                try:
                    raw_len = await self._read_exact(4)
                    length = int.from_bytes(raw_len, "big")
                    payload = await self._read_exact(length)
                    msg = json.loads(payload.decode("utf-8"))
                    # 服务端心跳 → 回复 pong
                    if msg.get("type") == "ping":
                        await self.send_inbound({"type": "pong"})
                        continue
                    if msg.get("type") in ("config_update", "sync_config"):
                        if self._on_config is not None:
                            self._on_config(msg)
                    elif self._on_outbound is not None:
                        self._on_outbound(msg)
                except asyncio.IncompleteReadError as e:
                    logger.warning("🔌 服务端断开连接: %s (received %d bytes, expected %d)",
                                   e, len(e.partial) if hasattr(e, 'partial') else 0,
                                   e.expected if hasattr(e, 'expected') else '?')
                    self._connected = False
                    await self._cleanup()
                except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                    logger.warning("⚠️ 连接异常 (%s: %s)", type(e).__name__, e)
                    self._connected = False
                    await self._cleanup()
                except (json.JSONDecodeError, AttributeError) as e:
                    logger.warning("📦 消息格式异常: %s", e)
                    self._connected = False
                    await self._cleanup()
            if self._should_run:
                backoff = min((backoff + 1) * self._reconnect_delay, 30.0)
                logger.info("%.1f 秒后重连 %s:%s ...", backoff, self._host, self._port)
                await asyncio.sleep(backoff)

    async def request_config(self) -> bool:
        return await self.send_inbound({"type": "sync_config"})

    async def send_inbound(self, data: dict[str, Any]) -> bool:
        if not self._connected or self._writer is None:
            logger.debug("未连接，丢弃入站消息")  # DEBUG 级别，不刷屏
            return False
        try:
            raw = json.dumps(data, ensure_ascii=False)
            payload = raw.encode("utf-8")
            size_mb = len(payload) / 1024 / 1024
            if size_mb > 0.5:
                logger.info("发送大消息: %.1f MB", size_mb)
            self._writer.write(len(payload).to_bytes(4, "big"))
            self._writer.write(payload)
            await asyncio.wait_for(
                self._writer.drain(), timeout=self._write_timeout,
            )
            return True
        except asyncio.TimeoutError:
            logger.warning("⏱️ 发送超时（%.1fs），数据可能过大 (%.1f MB)", self._write_timeout, size_mb)
            self._connected = False
            await self._cleanup()
            return False
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
