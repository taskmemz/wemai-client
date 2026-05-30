from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("wemai_client.sender")


class WeChatSender:
    def __init__(
        self,
        outbound_queue: queue.Queue,
        send_delay: float = 0.2,
        close_weixin: bool = False,
    ) -> None:
        self._queue = outbound_queue
        self._send_delay = send_delay
        self._close_weixin = close_weixin
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._dialog_windows: dict[str, Any] = {}
        self._on_post_send: Optional[Callable[[str], None]] = None
        self._on_pre_send: Optional[Callable[[str], None]] = None

    def set_dialog_windows(self, d: dict[str, Any]) -> None:
        self._dialog_windows = d

    def set_post_send_hook(self, hook: Callable[[str], None]) -> None:
        self._on_post_send = hook

    def set_pre_send_hook(self, hook: Callable[[str], None]) -> None:
        self._on_pre_send = hook

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="wx-sender")
        self._thread.start()
        logger.info("发送线程已启动")

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while self._running:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._do_send(item)
            except Exception as e:
                logger.error("发送失败: %s", e)

    def _do_send(self, item: dict) -> None:
        receiver = item.get("receiver", "")
        segments = item.get("segments", [])
        at_members = item.get("at_members", [])
        if not receiver or not segments:
            return

        # 通知 listener 暂停对该窗口的轮询
        if self._on_pre_send is not None:
            self._on_pre_send(receiver)

        # 统一走 pyweixin 发送（多选模式下 Edit 控件定位不可靠）
        self._do_send_via_pyweixin(receiver, segments, at_members)

        if self._on_post_send is not None:
            self._on_post_send(receiver)

    def _do_send_via_pyweixin(self, receiver: str, segments: list, at_members: list | None = None) -> None:
        from pyweixin import GlobalConfig, Messages
        GlobalConfig.close_weixin = self._close_weixin
        GlobalConfig.send_delay = self._send_delay
        has_at = bool(at_members)
        for seg in segments:
            sdata = seg.get("data", "")
            if not sdata:
                continue
            # 有 @ 高亮时去掉文本中的 @name，避免微信里重复显示
            if has_at and sdata.startswith("@"):
                seg_at = at_members
                clean = sdata
                for name in at_members:
                    clean = clean.replace(f"@{name}", "").strip()
                sdata = clean or sdata  # 如果清空后没内容了，保留原文本
            else:
                seg_at = []
            Messages.send_messages_to_friend(
                friend=receiver, messages=[sdata],
                at_members=seg_at,
                close_weixin=False,
            )
            logger.info("→ pyweixin [%s] %s @%s", receiver, sdata[:60], seg_at)
