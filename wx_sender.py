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

        dw = self._dialog_windows.get(receiver)
        if dw is None:
            logger.warning("未找到 [%s] 的独立窗口，尝试用 pyweixin 发送", receiver)
            self._do_send_via_pyweixin(receiver, segments, at_members)
            return

        # 通知 listener 暂停对该窗口的轮询
        if self._on_pre_send is not None:
            self._on_pre_send(receiver)

        # 将窗口带到前台，否则 type_keys 可能不生效
        try:
            dw.set_focus()
            time.sleep(0.2)
            dw.restore()
            time.sleep(0.1)
        except Exception:
            pass

        # @ 前缀已在 segments 中合成文本（@name ），直接输入即可
        for seg in segments:
            stype = seg.get("type", "text")
            sdata = seg.get("data", "")
            if stype != "text" or not sdata:
                continue
            try:
                # 多选模式下 UIA 树结构变化，逐级尝试找输入框
                edits = (
                    dw.descendants(control_type="Edit")
                    or dw.child_window(control_type="Edit")
                    or dw.descendants(control_type="Document")
                )
                if edits:
                    try:
                        edits[0].set_focus()
                    except Exception:
                        pass
                    edits[0].click_input()
                    edits[0].type_keys("^a{BACKSPACE}")  # 清空原有内容
                    edits[0].type_keys(sdata + "{ENTER}", pause=0.02)
                    logger.info("→ [%s] %s", receiver, sdata[:60])
                else:
                    logger.warning("[%s] 独立窗口中未找到输入框，fallback pyweixin", receiver)
                    self._do_send_via_pyweixin(receiver, segments)
                    return
            except Exception as e:
                logger.error("向 [%s] 发送失败: %s", receiver, e)
                self._do_send_via_pyweixin(receiver, segments)
                return

        if self._on_post_send is not None:
            self._on_post_send(receiver)

    def _do_send_via_pyweixin(self, receiver: str, segments: list, at_members: list | None = None) -> None:
        from pyweixin import GlobalConfig, Messages
        GlobalConfig.close_weixin = self._close_weixin
        GlobalConfig.send_delay = self._send_delay
        for seg in segments:
            sdata = seg.get("data", "")
            if sdata:
                Messages.send_messages_to_friend(
                    friend=receiver, messages=[sdata],
                    at_members=at_members or [],
                    close_weixin=False,
                )
                logger.info("→ pyweixin [%s] %s @%s", receiver, sdata[:60], at_members)
