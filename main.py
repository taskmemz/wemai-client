from __future__ import annotations

import asyncio
import logging
import os
import queue
import signal
import sys
import threading
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as cfg_mod
from ws_client import WsPluginClient
from wx_listener import WeChatListener
from wx_sender import WeChatSender
from wx_moments import WeChatMoments

logger = logging.getLogger("wemai_client.main")
_stop_event = threading.Event()


def setup_logging(log_cfg: cfg_mod.LogConfig) -> None:
    os.makedirs(os.path.dirname(log_cfg.file) or ".", exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, log_cfg.level.upper(), logging.INFO),
        format=log_cfg.format,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_cfg.file, encoding="utf-8"),
        ],
    )


def handle_signal(sig, frame) -> None:
    logger.info("收到信号 %s", sig)
    _stop_event.set()


class WeMaiClient:
    def __init__(self, cfg: cfg_mod.ClientConfig) -> None:
        self._cfg = cfg
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws = WsPluginClient(
            host=cfg.connection.server_host,
            port=cfg.connection.server_port,
            reconnect_delay=cfg.connection.reconnect_delay,
        )
        self._outbound_queue: queue.Queue = queue.Queue()
        self._sender = WeChatSender(
            outbound_queue=self._outbound_queue,
            send_delay=cfg.wechat.send_delay,
            close_weixin=cfg.wechat.close_weixin,
        )
        self._listener: Optional[WeChatListener] = None

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        logger.info("=" * 50)
        logger.info("WeMai Client - 微信 × MaiBot 桥接客户端")
        logger.info("服务器: %s:%s", self._cfg.connection.server_host, self._cfg.connection.server_port)
        logger.info("=" * 50)

        self._ws.set_outbound_handler(self._on_plugin_outbound)
        self._ws.set_config_handler(self._on_config_update)

        listener = WeChatListener(
            target_chats=self._cfg.wechat.target_chats,
            excluded=self._cfg.wechat.excluded,
            poll_interval=1.0,
            close_weixin=self._cfg.wechat.close_weixin,
            send_delay=self._cfg.wechat.send_delay,
            on_message=self._on_wechat_message,
            group_members=self._cfg.wechat.group_members,
            include_muted=self._cfg.wechat.include_muted,
        )
        self._listener = listener
        
        # Wire sender to listener's dialog windows
        self._sender.set_dialog_windows(listener.get_dialog_windows())
        self._sender.set_pre_send_hook(listener.begin_send)
        self._sender.set_post_send_hook(listener.mark_sent)
        
        self._sender.start()
        listener.start()

        ws_task = asyncio.create_task(self._ws.run())

        try:
            while not _stop_event.is_set():
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            self._listener = None
            listener.stop()
            self._sender.stop()
            await self._ws.stop()
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass
            logger.info("所有组件已停止")

    def _on_config_update(self, msg: dict) -> None:
        if self._listener is not None:
            self._listener.merge_remote_targets(
                enable_filter=msg.get("enable_filter", False),
                group_list=msg.get("group_list", []),
                private_list=msg.get("private_list", []),
            )
            logger.info("已同步 Adapter 配置: filter=%s, groups=%s, private=%s",
                         msg.get("enable_filter"), msg.get("group_list"), msg.get("private_list"))

    def _handle_moment_command(self, msg: dict) -> None:
        """处理来自 Adapter 的朋友圈命令"""
        req_id = msg.get("request_id", "")
        cmd = msg.get("type", "")
        
        if cmd == "moment_read":
            limit = msg.get("limit", 10)
            try:
                posts = WeChatMoments.read_recent(number=limit)
            except Exception as e:
                logger.error("读取朋友圈失败: %s", e)
                posts = []
            logger.info("朋友圈读取: %d 条", len(posts))
            self._send_moment_response(req_id, {"moments": posts, "count": len(posts)})
        
        elif cmd == "moment_post":
            text = msg.get("text", "")
            try:
                ok = WeChatMoments.post_moment(text=text)
                logger.info("发布朋友圈: %s → %s", text[:40], "成功" if ok else "失败")
            except Exception as e:
                logger.error("发布朋友圈失败: %s", e)
                ok = False
            self._send_moment_response(req_id, {"success": ok, "text": text})

    def _send_moment_response(self, req_id: str, data: dict) -> None:
        """发送朋友圈响应回 Adapter"""
        payload = {"type": "moment_response", "request_id": req_id, **data}
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._ws.send_inbound(payload),
                self._loop,
            )

    def _on_wechat_message(self, msg: dict) -> None:
        logger.info("微信: [%s] %s: %s", msg["chat"], msg["sender"], msg["content"][:60])
        if self._loop is not None and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self._ws.send_inbound(msg),
                self._loop,
            )
            future.add_done_callback(lambda f: logger.info(
                "发送到 Adapter %s", "成功" if f.result() else "失败"
            ))

    def _on_plugin_outbound(self, msg: dict) -> None:
        msg_type = msg.get("type", "")
        if msg_type == "ack":
            logger.info("Adapter ACK: original=%s success=%s error=%s",
                        msg.get("original_type"), msg.get("success"), msg.get("error", "无"))
            return
        
        # 朋友圈命令处理
        if msg_type in ("moment_read", "moment_post"):
            self._handle_moment_command(msg)
            return

        receiver = msg.get("receiver", "")
        segments = msg.get("segments", [])
        at_members = msg.get("at_members", [])
        if not receiver or not segments:
            return
        self._outbound_queue.put({"receiver": receiver, "segments": segments, "at_members": at_members})
        for seg in segments:
            logger.info("MaiBot → [%s]: %s", receiver, seg.get("data", "")[:60])


async def async_main() -> None:
    cfg = cfg_mod.load_config()
    setup_logging(cfg.log)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    client = WeMaiClient(cfg)
    await client.run()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.getLogger("wemai_client.main").critical("异常退出: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
